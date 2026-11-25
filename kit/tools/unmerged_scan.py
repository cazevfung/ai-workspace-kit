#!/usr/bin/env python
"""「done 但没有合并记录」按现场分两栏：真待合并 / 仓库无改动。

为什么要有这一层（AW-147）：AW-146 只改了本机一条调度的提示词，仓库里一个文件都
没动。它提交时记下的 `head_sha` 就等于当时 master 的 head，也没有 `ticket/AW-146`
分支——于是它一进 `done` 就被 `list_unmerged` 算成「真待合并」，而
`auto_merge --ticket AW-146` 第 2 步就停（源分支不存在），`next_action` 每轮都报
「合并 AW-146」，后面排着的单永远派不出去。

两栏的差别只有一条，而且当场能核：**这次尝试有没有造出提交**（提交记下的
`head_sha` 与 `base_sha` 相同，说明交上去的还是分叉点那棵树），**同时仓库里没有
这张单的分支**。两条都成立 → 没有可合的树，它不是「还没合」，是「本来就不在仓库
里」。这种单不该写一条合并记录去冒充（那是伪造），也不该塞进 `archived`
（archived 的语义是「做完了但账对不上」，它的账是清楚的）。

判据不能拿 `head_sha` 与目标分支的 head 比（AW-148）：那个值会随着每一张单合并进
master 往前移动，于是当时判对的单在下一张单合并之后又退回去。AW-146 提交时
`head_sha = base_sha = 2e911e84`，与当时的 master head 相等；AW-147 一合进 master
（head 变 5aa136d），它就被重新算成「真待合并」，`next_action` 又开始派「合并
AW-146」这个永远走不通的动作。`head_sha == base_sha` 说的是这条提交自己的事实，
不随别人的合并移动。

判定只在能**确认**时成立。git 跑不动、目标分支读不到、源分支存在（哪怕领先 0 个
提交）、`head_sha` 与 `base_sha` 不等或 `base_sha` 为空（老提交，说不清有没有造过
提交）——任何一条都返回 None，调用方把它归回「真待合并」。**例外是非 git 项目
（AW-151）**：git 当面确认项目根不是仓库、且提交的两棵树都是 NULL（`repo_kind: "non_git"`）
时，那里本来就没有可合的树，归「仓库无改动」并在依据里写「项目根不是 git 仓库」；
非 git 项目却带着 sha（编的占位值）仍是 None，留在真待合并让人看见；git 问不出来
（不在、读不动）也不当成「不是仓库」。把不确定的单藏进另一栏，比多列一行糟得多：
清单的全部价值是「看得见」。

只读：本模块只跑 `git rev-parse`，不写库、不动分支。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

# 目标分支的缺省值就这一处：`auto_merge` 的 `--target` 与此同源，两边不各写一个。
TARGET_BRANCH = "master"

# 分栏名与 `skipped` 里的 reason 用同一个字符串：人在输出里看到的词与结构化结果里的
# 词对得上，才不用在中途翻译一次。
NO_REPO_CHANGE = "no_repo_change"


class RepoProbe:
    """在一个或多个仓库上做只读探测，结果按 (仓库, 参数) 缓存。

    缓存的理由只有一条：`--list` 会给同一张项目根下的每张单都问一次同样的两个 ref，
    不缓存就是 N 次同样的 `git rev-parse`。每次列表/扫描都新建一个，不跨调用共用——
    共用会让「刚刚才建出来的分支」在缓存里还是不存在。
    """

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self._cache: dict[tuple[str, tuple[str, ...]], subprocess.CompletedProcess | None] = {}

    def _git(self, repo, *args):
        key = (str(Path(repo)), args)
        if key in self._cache:
            return self._cache[key]
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(repo),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=self.timeout,
            )
        except (OSError, subprocess.SubprocessError):
            # git 不在、路径读不动、超时：这是「不知道」，不是「没有分支」。
            proc = None
        self._cache[key] = proc
        return proc

    def is_repo(self, repo) -> bool:
        """这个目录是不是 git 仓库的检出（工作树也算）。"""
        return self.repo_state(repo) == "repo"

    def repo_state(self, repo) -> str:
        """这个目录的仓库状态：`repo` / `not_repo` / `unknown`。

        三种必须分开（AW-151）：`not_repo` 是 git 当面给出的结论（非 git 项目根本没有
        可合的树，归「仓库无改动」）；`unknown` 是问不出来——git 不在、路径读不动、
        超时，不能当成「不是仓库」。把不确定的单藏进另一栏，比多列一行糟得多。
        """
        proc = self._git(repo, "rev-parse", "--git-dir")
        if proc is None:
            return "unknown"
        if proc.returncode == 0:
            return "repo"
        message = ((proc.stderr or "") + (proc.stdout or "")).lower()
        if "not a git repository" in message:
            return "not_repo"
        return "unknown"

    def ref_sha(self, repo, ref) -> str | None:
        """ref 指向的提交；ref 不存在、仓库不对、git 不在，都是 None。"""
        proc = self._git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if proc is None or proc.returncode != 0:
            return None
        sha = proc.stdout.strip()
        return sha.lower() or None

    def basis(self, repo, branch, head_sha, base_sha,
              target: str = TARGET_BRANCH) -> dict | None:
        """判成「仓库无改动」时返回可在同一行写出的依据，否则 None。

        返回的 `basis` 一句话必须能独立复核：`head_sha` 与 `base_sha` 是同一个短 sha
        （这次尝试没有造出提交）+ 分支不存在（连「没合的树」都没有）。人在清单上读到
        这一行就够了，不必再去跑一遍 git 才知道这条结论从哪来。

        `base_sha` 必填：它是「从哪分出来的」。为空（schema 10 之前的提交）就说不清
        这次有没有造出提交，一律留在真待合并。

        项目根不是 git 仓库是另一回事（AW-151）：那里没有分支、没有提交，也就从来没有
        可合的树。只有「两棵树都为 NULL」才是按协议提交的非 git 成果；带着 sha 的非 git
        项目说明有人塞了占位值，留在真待合并让人看见。git 当面确认「不是仓库」且两棵树
        都是 NULL 时直接给 `no_repo_change` 的依据；问不出来（git 不在、路径读不动）
        仍是 None，归回真待合并。
        """
        if not repo:
            return None
        state = self.repo_state(repo)
        if state == "unknown":
            return None
        if state == "not_repo":
            if head_sha or base_sha:
                # 非 git 项目不该有树。带着 sha 就是编的，别把它藏进本栏。
                return None
            return {
                "reason": NO_REPO_CHANGE,
                "ticket_id": None,
                "branch": branch,
                "target_branch": target,
                "head_sha": None,
                "base_sha": None,
                "basis": f"项目根不是 git 仓库（{repo}），两棵树都是 NULL，没有可合的树",
            }
        if not head_sha:
            return None
        head = str(head_sha).strip().lower()
        if not head:
            return None
        base = str(base_sha or "").strip().lower()
        if not base or head != base:
            # 两个 sha 不同 = 这次真造过提交；base 为空 = 说不清。两条都不是本栏。
            return None
        if self.ref_sha(repo, f"refs/heads/{target}") is None:
            # 目标分支读不到：这个检出可能根本不对，宁可留在真待合并。
            return None
        if self.ref_sha(repo, f"refs/heads/{branch}") is not None:
            # 分支还在：这是「真的还没合」（或者合并成功、只差记录），两条都不是本栏。
            return None
        short = head[:8]
        return {
            "reason": NO_REPO_CHANGE,
            "ticket_id": None,
            "branch": branch,
            "target_branch": target,
            "head_sha": head,
            "base_sha": base,
            "basis": f"head_sha 与 base_sha 相同（{short}，这次尝试没有造出提交），"
                     f"且仓库里没有 {branch} 分支",
        }

    def no_repo_change(self, repo, ticket_id, head_sha, base_sha,
                       target: str = TARGET_BRANCH) -> dict | None:
        """`basis` 的工单版：把 ticket_id 一起写进依据里，清单直接用它。"""
        found = self.basis(repo, f"ticket/{ticket_id}", head_sha, base_sha, target)
        if found is not None:
            found["ticket_id"] = ticket_id
        return found


def project_roots(center) -> dict[str, str]:
    """项目 id → 项目根。读不到就返回空表：分类会退回「真待合并」，不会藏单。

    `--list` 是只读清单，项目表读不动时整体失败没有意义——那会让「本来还能看见的
    待合并单」也看不见。空表只会让这一栏是 0 张。
    """
    try:
        reply = center.execute("list_projects", {})
    except Exception:  # noqa: BLE001  读侧降级：清单不该因为项目表读不动就整段不出
        return {}
    if not isinstance(reply, dict) or not reply.get("ok"):
        return {}
    items = (reply.get("result") or {}).get("items") or []
    roots = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        # `list_projects` 的键是 `project_id`；兼容 `id` 是为了不把一个纯拼写差异
        # 变成「整列 0 张」——那会静静地把这一栏关掉。
        pid = item.get("project_id") or item.get("id")
        if pid and item.get("root"):
            roots[pid] = item["root"]
    return roots
