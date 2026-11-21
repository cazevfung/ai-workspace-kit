#!/usr/bin/env python
"""验收通过后自动合并：跑通全量测试才合，红了回滚到预合并 tag。

用法（仓库根 {{WORKSPACE}}）：

    python tools/auto_merge.py --list
    python tools/auto_merge.py --ticket AW-018 [--branch ticket/AW-018] [--target master] [--dry-run]
    python tools/auto_merge.py --ticket AW-018 --record-only

顺序（任何一步不满足就非零退出，并写明是哪一步、怎么补）：

0. 合并互斥：在 **git 公共目录**（`git rev-parse --path-format=absolute --git-common-dir`）
   里的 `auto-merge.lock` 取锁（写 pid、开始时间、工单号）。主检出与它的每个工作树
   解析到同一个绝对路径，所以共用一把锁；按检出根算会让两边的锁互不可见。
   已经被别人占着就非零退出，打印锁的绝对路径与占用者：在合哪张单、从什么时候开始、
   已经持续多久；
   持有者已经不在了（pid 不在进程表里，或超过 2 小时阈值）就当残留锁显式接管，
   输出里写明接的是哪一把。锁在第 1 步之前取、第 7 步之后释放，释放放在 finally 里。
1. 读工单：必须 `state=done`，且 `merges` 为空（已有记录就拒绝，提示已合并）。
   同一步的收尾判**回执**：该单 `notices` 空数组就按 `NO_NOTICE` 非零退出，并指向
   `tools/notice_payload.py`。库里 `merge_ticket` 本来也有这道门，但它在第 7 步——
   那时 `git merge` 已经落到目标分支上去了，`--record-only` 又会撞回同一句话。
   挪到这里之后，这道判在**任何写入之前**（比冲突预演的 merge-tree、回退点 tag、
   journal、临时 worktree 都早），被拒时 master、tag、journal、工作树与分支一样都没动。
   `--dry-run` 例外：预演本来就什么都不写，这道判在那里只降为一条警告，好让预演
   照样走完冲突预演与临时 worktree 测试（真合并仍然过不去）。
2. 项目配置与前置检查：先读目标项目的 `.agents/ticket-config.json`——缺文件、缺
   `test_cmd` 或 `target_branch` 就停在这一步，提示跑 `tools/add_project.py`，不回落
   到任何默认测试命令；`--target` 不传时用配置里的 `target_branch`。然后当前检出必须
   在目标分支上（否则方向反了：`git merge` 会把目标分支合进当前分支）；**已跟踪文件**无改动（`git status --porcelain` 里除 `??` 之外为空）；
   目标分支存在、源分支存在、源分支领先目标（`rev-list --count target..branch > 0`）。
   未跟踪文件本身不拦（它们不参与这次合并），但会逐个列出，并注明「这些文件没有被合进去」。
   第 2 步收尾第一件事是**比对两棵树**：工单最新一次提交的 `head_sha`（被验收的那棵树）
   与分支 tip（`git rev-parse refs/heads/<源分支>`，要合的那棵树）不等就一步不动地失败。
   它以前不存在，所以验收之后再提交一次也能合进去，而库里查不出来（R-009 目标第 2 条）。
   老提交没有 `head_sha`（NULL）时不堵死也不静默放行：输出一条警告，合并记录按
   `unverified` 落库，事后查得到这单没核过。见 `guard_reviewed_sha`。
   第 2 步收尾还要预检一件事：**这次合并要新增的路径**若与某个未跟踪文件同名
   （分隔符与大小写按 Windows 口径归一，内容相同也算），merge 必然会以
   `untracked working tree file would be overwritten` 拒绝，所以在这里就停下、
   指名文件并给出补救；交集为空时行为与以前完全一样。见 `guard_untracked_overwrite`。
3. 冲突预演：`git merge-tree`；有冲突就列出文件并中止，不自动解冲突。
4. 打回退点 `pre-merge-<ticket>`（已存在则拒绝：不覆盖旧回退点）。
5. 临时 worktree 里先合一次并跑项目配置里的 `test_cmd`；红就删临时 worktree、
   退出非零——此时目标分支一动未动。
6. 绿了在真实检出上 `git merge --no-ff <branch>`；合并后再跑一次配置里的测试，
   再红就 `git reset --hard <tag>` 回滚。
7. 成功：调 `merge_ticket` 写回合并提交、目标分支与测试汇总行。

`--dry-run`：跑到第 5 步就停。不建 tag（避免占掉真实合并要用的名字）、
不碰目标分支、不写合并记录。回执预检在 dry-run 下只输出警告：预演要能预演到底，
包括冲突预演与临时 worktree 里那场全量测试。

`--record-only`：收口「`git merge` 已经成功、合并记录还没写」这种半截状态。
AW-058 就断在这里：master 上已经有那次合并提交，`merges` 里没有这一行，公共目录里
留着一把 pid 早已不在的锁。默认路径在这种现场**一步都走不动**——第 2 步要求「源分支
领先目标」，而源分支已经被合进去了，领先数正好是 0。这条路径把那道判据反过来问：

0. 锁照旧（残留锁会被接管，因为「合并进程死了」正是这种现场的常态）。
1. 工单照旧：必须 `done`，且 `merges` 为空——已有记录就报 `ALREADY_MERGED`。
2. 前置检查照旧（当前检出在目标分支上、已跟踪文件无改动、目标与源分支都存在），
   但「源分支领先目标」换成「源分支已全在目标里」：
   `git merge-base --is-ancestor <源> <目标>` 为真，且 `rev-list --count <目标>..<源>` 为 0。
   这一步不预检未跟踪文件：这次不再往工作区写任何路径。
3. 在目标的祖先里找那次合并提交：`rev-list --parents <目标>` 逐行比对，取 `parent2`
   正好等于源分支 tip 的那一个。找不到（例如当时是快进合并，根本没有合并提交）就
   非零退出、不写记录——记录里的 commit 必须是真合过的那一个，不能拿 HEAD 顶替。
4. 在**当前检出**（已经在目标分支上）跑 `python tools/run_tests.py`，全绿才写记录；
   红就非零退出，不写记录、不清理工作树与分支。
5. `merge_ticket` 写回记录，`tests` 写第 4 步实跑的汇总行。
6. 记录写成功之后清理：`git worktree remove .worktrees/<工单号>`（存在才删）、
   `git branch -d ticket/<工单号>`。清理失败只报告，不把已经写成功的记录当成失败——
   那时该做的是人工清理，不是重跑（重跑会被第 1 步的 `ALREADY_MERGED` 挡回来）。

正常合并路径还会写一份 `.data/merge-journal/<工单号>.json`（合并提交、目标分支、源分支、
回退点、开始时间、做到第几步）：合并提交落在工作区之后、合并后的测试之前写，测试绿了
更新一次 step，`merge_ticket` 成功后删除；测试红回滚之后也删除，删前把 step 改成
`rolled-back`。这样进程被杀之后，「这次合并做到第几步」不再只能靠猜锁文件和 git log。

环境异常（找不到 git 命令、git 超时、锁写不进去）不是某一步的判定结果，而是
“这次根本没法开始”。它们一律收成本工具那三行 Fail：停在哪一步、到底怎么回事、
怎么补；并且「找不到 git 命令」与「不是 git 仓库」是两句话、两种补救，不共用一个说法。

为什么要有这个脚本：`done` 只说「验收通过」，不说「结果进了 master」。AW-011
那次合并靠人工 `merge-tree` + 手写 `-X ours` + 逐字节比对，之后留下 19 处旧路径；
plan-005 的起因就是功能验收了、代码还留在分支上。所以把合并做成一条命令，
且「测试红」必须是**目标分支一动未动**的失败，而不是合完了才报错。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# 与 work_gate 共用「中心库只能被发现、不能被新建」的口径，避免两套规则。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import work_gate  # noqa: E402

STEP_LOCK = "第 0 步 · 合并互斥锁"
STEP_CONFIG = "第 2 步 · 项目配置"
STEP_TICKET = "第 1 步 · 读工单"
STEP_NOTICE = "第 1 步 · 回执预检"
STEP_CHECKS = "第 2 步 · 前置检查"
STEP_SHA = "第 2 步 · 被验收的 sha 与要合的 sha"
STEP_UNTRACKED = "第 2 步 · 未跟踪文件预检"
STEP_CONFLICT = "第 3 步 · 冲突预演"
STEP_TAG = "第 4 步 · 打回退点 tag"
STEP_PREVIEW = "第 5 步 · 临时 worktree 试合与测试"
STEP_MERGE = "第 6 步 · 真实合并"
STEP_RECORD = "第 7 步 · 写合并记录"
# --record-only 才用到的两步：找那次合并提交、在当前检出上跑全量测试。
# 步骤名带上前缀“第 2 步 / 第 5 步”，是为了让人一眼看出它在正常流程里对应哪一段。
STEP_FIND = "第 2 步 · 找合并提交（--record-only）"
STEP_RECORD_TESTS = "第 5 步 · 全量测试（--record-only）"
STEP_CLEANUP = "第 7 步 · 清理工作树与分支"


class Fail(Exception):
    """一步没通过。带上是哪一步、为什么、以及能照做的补救。"""

    def __init__(self, step: str, reason: str, fix: str = ""):
        super().__init__(f"{step}：{reason}")
        self.step = step
        self.reason = reason
        self.fix = fix


def git_env(extra=None) -> dict:
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    for key, value in (extra or {}).items():
        # 大小写无关地替换同名项：Windows 上 `os.environ` 里的键名可能是 `Path`，
        # 直接 `env["PATH"] = …` 会留下两条同名（不同大小写）的项，子进程拿到哪条
        # 全靠运气。
        for existing in [name for name in env if name.upper() == key.upper()]:
            del env[existing]
        env[key] = value
    return env


# 环境异常时的两句话。它们不能混用：说「不是 git 仓库」而实际是「没装 git」，
# 人会去翻目录而不是去看 PATH；反过来也一样。所以原因写清哪一种，补救各给一句。
FIX_GIT_MISSING = (
    "确认 git 已安装并在 PATH 里：在命令行跑 `git --version` 应有版本号输出；"
    "装好后重开终端（PATH 的改动不会进已经开着的会话）再跑一次。"
)
FIX_NOT_A_REPO = (
    "在仓库根跑；--repo 指向含 .git 的目录。锁放在 git 公共目录里，"
    "主检出与工作树共用同一把。"
)


def git_missing_fail(step: str, exc) -> Fail:
    """「找不到 git 命令」不是「不是 git 仓库」：两句话分开，补救也不同。"""
    return Fail(
        step,
        f"找不到 git 命令：PATH 里没有可执行的 git（{exc}）。",
        FIX_GIT_MISSING,
    )


def git_timeout_fail(step: str, args, timeout) -> Fail:
    """git 卡住不动比报错更像正常：不解释清楚，人会以为是在跑测试。"""
    return Fail(
        step,
        f"git {' '.join(args)} 超过 {timeout} 秒没有返回，已中止等待。",
        "确认没有别的进程占着仓库（另一次合并、手动 git 命令、编辑器留下的 index.lock）；"
        "清掉残留锁后重跑。本工具不替人杀进程，也不会在超时后继续改分支。",
    )


def git(repo, *args, step, check=True, env=None, timeout=600):
    """跑一条 git 命令。check=True 时非零退出直接转成带步骤名的 Fail。

    git 不存在（FileNotFoundError）与 git 超时（TimeoutExpired）不是 git 的判断结果，
    是环境问题，所以不论 check 与否都转成三行 Fail——否则整段 traceback 打在人脸上，
    而真正要做的事只有一件：把 git 装好或把卡住的 git 清掉。
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=git_env(env),
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise git_missing_fail(step, exc)
    except subprocess.TimeoutExpired:
        raise git_timeout_fail(step, args, timeout)
    if check and proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip() or "(无输出)")
        raise Fail(step, f"git {' '.join(args)} 退出 {proc.returncode}：{detail}")
    return proc


def git_out(repo, *args, step, env=None) -> str:
    return git(repo, *args, step=step, env=env).stdout.strip()


def exists(repo, ref, env=None) -> bool:
    return git(repo, "rev-parse", "--verify", "--quiet", ref, step="", check=False, env=env).returncode == 0


def run_tests_in(where, *, python, test_cmd, env, timeout=1800, step=STEP_PREVIEW):
    """在 where 里跑项目配置里的测试命令，返回 (是否通过, 汇总行, 完整输出)。

    `test_cmd` 必须由 `load_project_config` 读出来：没有配置就不该走到这里。
    这里不再回落 `tools/run_tests.py`——回落会让「项目根本没配测试」看起来像跑过了。
    """
    if not test_cmd:
        raise Fail(
            step,
            "项目配置里没有 test_cmd，无法确认合并前测试跑什么。",
            "先用 `python tools/add_project.py <仓库> --test-cmd \"...\"` 写配置；"
            "不要用一条空命令让合并看起来通过了。",
        )
    command = list(test_cmd)
    if command[0] in ("python", "python3", "py"):
        command[0] = python
    try:
        proc = subprocess.run(
            command,
            cwd=str(where),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=git_env(env),
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise Fail(
            step,
            f"跑不了测试命令：找不到 {command[0]}（{exc}）。",
            f"检查 .agents/ticket-config.json 的 test_cmd：{command}；"
            "本工具用的是 --python 指定的解释器（默认当前解释器）。",
        )
    except subprocess.TimeoutExpired:
        raise Fail(
            step,
            f"测试超过 {timeout} 秒没有跑完，已中止等待。",
            "先手动跑一次配置里的 test_cmd 看是哪一处卡住；"
            "修好前不要合并，目标分支此时没有被改动。",
        )
    output = (proc.stdout or "") + (proc.stderr or "")
    summary = ""
    for line in output.splitlines():
        if line.startswith("汇总："):
            summary = line.split("：", 1)[1].strip()
    if not summary:
        summary = next((line.strip() for line in reversed(output.splitlines()) if line.strip()), "(无输出)")
    return proc.returncode == 0, summary, output


def load_project_config(repo) -> dict:
    """读 `<repo>/.agents/ticket-config.json`，返回已验证的配置。

    为什么不让它可选：以前合并只认「跑 tools/run_tests.py」+ 默认 master。业务项目
    接进工作区之后，测试命令与目标分支各不相同；缺配置时静默回落，会让一场根本没
    跑的测试被写成合并记录里的「通过」。所以缺文件、缺字段、类型不对一律停在这里，
    且指向 `add_project.py`，而不是猜一个默认值。
    """
    repo = Path(repo).resolve()
    path = repo / ".agents" / "ticket-config.json"
    fix = (
        f"先接项目：`python tools/add_project.py {repo.as_posix()}`（可加 "
        "`--test-cmd \"...\"` 指定测试命令）。它会写 .agents/ticket-config.json，"
        "并把 core.hooksPath 指向套件 .githooks。"
    )
    if not path.is_file():
        raise Fail(STEP_CONFIG, f"目标项目缺 .agents/ticket-config.json：{path}", fix)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Fail(STEP_CONFIG, f"读不懂 {path}：{exc}", fix) from exc
    if not isinstance(data, dict):
        raise Fail(STEP_CONFIG, f"{path} 顶层必须是对象。", fix)
    test_cmd = data.get("test_cmd")
    if not isinstance(test_cmd, list) or not test_cmd or not all(isinstance(part, str) and part for part in test_cmd):
        raise Fail(
            STEP_CONFIG,
            f"{path} 的 test_cmd 必须是非空字符串数组（例如 [\"python\", \"tools/run_tests.py\"]）。",
            fix,
        )
    target = data.get("target_branch")
    if not isinstance(target, str) or not target.strip():
        raise Fail(STEP_CONFIG, f"{path} 缺 target_branch（非空字符串，例如 master / main）。", fix)
    timeout = data.get("test_timeout", 1800)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise Fail(STEP_CONFIG, f"{path} 的 test_timeout 必须是正整数秒数。", fix)
    return {
        "path": path,
        "test_cmd": [str(part) for part in test_cmd],
        "target_branch": target.strip(),
        "test_timeout": timeout,
        "project_id": data.get("project_id"),
    }


def open_center(db_path=None, step=STEP_TICKET):
    """发现并打开中心库；不新建。"""
    db = db_path or work_gate.find_db()
    if db is None or not Path(db).is_file():
        raise Fail(
            step,
            "中心库没找到。",
            "检查 ~/.agents/center.json 的 db 字段指向的库是否存在；不要为了让合并跑起来新建一个库。",
        )
    try:
        center = work_gate.load_center()(db)
    except SystemExit as exc:  # load_center 用 SystemExit 报「找不到代码目录」
        raise Fail(step, f"找不到 task_center 代码目录：{exc}")
    return center, Path(db)


def open_center_for_project(repo, db_path=None, step=STEP_TICKET):
    """按仓库路径反查项目归属，返回 (center, project_id)。归属不许指定，只能反查。"""
    center, db = open_center(db_path, step=step)
    found, error = work_gate.registered_project(db, repo)
    if error is not None:
        raise Fail(step, f"{error['code']}：{error['message']}")
    if found is None:
        raise Fail(
            step,
            f"{Path(repo).resolve()} 不属于任何已注册项目。",
            "先 register_project 把这个仓库登记成一个项目，再建工单。",
        )
    return center, found["project_id"]


def center_ticket_reader(center, project_id):
    """返回一个 ticket_id -> 工单详情的读取函数。"""

    def read(ticket_id):
        reply = center.execute("get_ticket", {"project_id": project_id, "ticket_id": ticket_id})
        if not reply["ok"]:
            error = reply["error"]
            raise Fail(
                STEP_TICKET,
                f"读不到工单 {ticket_id}：{error['code']} {error['message']}",
                "核对工单号与项目归属；工单不存在就先 create_ticket。",
            )
        return reply["result"]

    return read


def center_merge_recorder(center):
    """返回一个把合并记录写回中心库的函数（调 merge_ticket）。"""

    def record(payload):
        reply = center.execute("merge_ticket", payload, request_id=f"auto-merge-{payload['ticket_id']}")
        if not reply["ok"]:
            error = reply["error"]
            fix = (
                "合并本身已经发生（改动已在目标分支上）。先核对 git log，再用 "
                "--record-only 收口：它会在目标的祖先里找到那次合并提交、跑完全量测试才写记录，"
                "写完再自己清工作树与分支。手工补记录与重跑整个工具都不用做——"
                "重跑会因为检出已领先而被拦在第 2 步。"
            )
            if error["code"] == "NO_NOTICE":
                # 这句原本指向 `--record-only`，而人在这种现场正是从 `--record-only`
                # 走进来的——指回去等于没说。要补的是回执，不是重跑同一个命令。
                ticket_id = payload["ticket_id"]
                fix = (
                    "先补一条回执：`python tools/notice_payload.py --ticket "
                    f"{ticket_id}` 出两段 JSON，`send_to_session` 带 `target_session_id` 发回"
                    "协调会话，再 `record_notice` 落库（送不到就记 `failed` 并写清原因，"
                    "`failed` 也算走过回执）；补完再跑本命令。"
                    "合并本身已经发生，不要重跑整个合并工具（会被第 2 步拦住）。"
                )
            raise Fail(
                STEP_RECORD,
                f"merge_ticket 拒绝：{error['code']} {error['message']}",
                fix,
            )
        return reply["result"]

    return record


def list_unmerged_rows(center, out=None):
    """每轮开工先看：已验收但没有合并记录的工单。"""
    emit = out or print
    reply = center.execute("list_unmerged", {})
    if not reply["ok"]:
        error = reply["error"]
        raise Fail(STEP_TICKET, f"list_unmerged 失败：{error['code']} {error['message']}")
    items = reply["result"]["items"]
    if not items:
        emit("没有「已验收但未合并」的工单。")
        return []
    emit(f"已验收但没有合并记录的工单（{len(items)} 张）：")
    for item in items:
        emit(f"  {item['id']}\t{item['project_id']}\t{item['title']}")
    emit("合并用：python tools/auto_merge.py --ticket <编号>")
    return items


def preview_conflict(repo, target, branch, out, env=None):
    """第 3 步：用 merge-tree 预演，有冲突就带文件名中止。

    merge-tree 太老（不认识 --write-tree）时不硬失败：第 5 步的临时 worktree
    还会再合一次，冲突在那里同样会被拦下，只是晚一步。
    """
    proc = git(
        repo, "merge-tree", "--write-tree", "--name-only", "--no-messages", target, branch,
        step=STEP_CONFLICT, check=False, env=env,
    )
    if proc.returncode == 0:
        out("冲突预演：无冲突。")
        return
    if proc.returncode == 1:
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        files = lines[1:] if len(lines) > 1 else lines
        raise Fail(
            STEP_CONFLICT,
            f"源分支与目标分支有冲突：{', '.join(files) if files else '(未列出文件名)'}",
            "先在本地把冲突解掉并提交到源分支，再跑一次；本工具不自动解冲突，也不会替你做 merge 策略决策。",
        )
    out("冲突预演：git merge-tree 不可用，跳过（第 5 步的临时 worktree 会再合一次）。")


def split_status(status: str) -> tuple[list[str], list[str], list[str]]:
    """把 `git status --porcelain` 的输出分成三类：已跟踪改动、未跟踪、被忽略。

    判定只看**已跟踪文件**：并行开发线留在工作区里的未跟踪文件（`.agents/` 下的
    技能脚本、还没纳入版本库的 PRD/计划）不参与这次合并，不该把合并整体拦住。
    返回 (tracked, untracked, ignored)，每项都是去掉状态前缀的路径；纯函数，好写测试。
    """
    tracked: list[str] = []
    untracked: list[str] = []
    ignored: list[str] = []
    for line in status.splitlines():
        if not line.strip():
            continue
        code, rest = line[:2], line[3:] if len(line) > 3 else ""
        path = rest.strip().strip('"') or line.strip()
        if code == "??":
            untracked.append(path)
        elif code == "!!":
            ignored.append(path)
        else:
            # 重命名/复制的行形如 `R  old -> new`，保留整行更不容易看漏。
            tracked.append(path)
    return tracked, untracked, ignored


# --- 第 2 步的收尾：未跟踪文件与「合并要新增的路径」是否撞名 ----------------
#
# 为什么单拿一件事出来说：AW-049 要新增一份需求文档，而主检出里
# 恰好有一份**同名**的未跟踪文件。那份文件与分支上的新文件逐字节相同，`git merge` 仍然
# 以 `untracked working tree file would be overwritten` 拒绝。所以这不是偶发：合并要往
# 工作区写出这个路径，工作区里已经有一个 git 不认识的同名文件，git 必须先删掉它才写得进去，
# 而删掉用户没提交的东西不是 git 敢做的事。必然冲突。
#
# 以前这件事只在第 6 步真合并时才炸：那时回退点已经打了、临时 worktree 合过一次、全量
# 测试跑了两轮，人看到的却是一句原始 git 报错。这步预检把它提前成一个指名点姓、带补救的
# 失败，而且**只看只报**——删或搬用户文件不可逆，该由人决定挪到哪。


def normalize_git_path(path: str) -> str:
    """比较用的路径口径：与 git 在 Windows 上看到的那个路径一致。

    分隔符统一成 `/`（git 的输出一直是 `/`，但人手里的路径可能带 `\\`）；Windows 的
    NTFS 默认不分大小写，git 在那里也把 `Docs/Guide.md` 与 `docs/guide.md` 当成同一个
    文件。只用于比较，不要拿它去落盘——落盘要用工作区里的原名。
    """
    text = str(path).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/").casefold()


def untracked_overlap(added, untracked) -> list[str]:
    """`added` ∩ `untracked`。纯函数，命中项按**工作区里的原名**返回。

    两条都必须归一：AW-049 里那份未跟踪文件与分支新增的文件逐字节相同，照样被拒，
    所以不能靠比内容放过；Windows 上大小写与斜杠方向都不是区分两个文件的东西。
    """
    index: dict[str, str] = {}
    for path in untracked:
        index.setdefault(normalize_git_path(path), str(path))
    hits: list[str] = []
    seen: set[str] = set()
    for path in added:
        key = normalize_git_path(path)
        hit = index.get(key)
        if hit is not None and key not in seen:
            seen.add(key)
            hits.append(hit)
    return hits


def untracked_worktree_files(repo, env=None) -> list[str]:
    """工作区里既没被跟踪、也没被 .gitignore 忽略的文件，逐条列出（不折叠目录）。

    不能用第 2 步展示用的 `git status --porcelain`：它默认把未跟踪目录压成 `scratch/`
    一行，拿那个去比路径，`scratch/notes.md` 这种撞名会整个漏掉。被忽略的文件不算——
    git 合并时允许覆盖它们，不会因此拒绝。`-z` 是必需的：不带它，git 会把中文等
    非 ASCII 路径转成 `"PRD\\346\\211..."`，而 AW-049 那份文件正是一个中文名。
    """
    proc = git(
        repo, "ls-files", "-z", "--others", "--exclude-standard",
        step=STEP_UNTRACKED, env=env,
    )
    files = [item for item in proc.stdout.split("\0") if item]
    return [path for path in files if not is_lock_artifact(path, repo)]


def target_new_paths(repo, target, branch, env=None) -> list[str]:
    """这次合并会**新建**在工作区里的路径：分支相对 merge-base 新增的文件。

    用三点 `target...branch` 取 merge-base，与 merge 实际应用的那段改动一致。
    `--no-renames` 是必需的：git 默认开改名检测，被认成改名的新路径不会出现在
    `--diff-filter=A` 里，可合并照样要把它写到工作区。`-z` 同理：不带它，中文路径会
    被转义成八进制，与工作区里的原名对不上。
    """
    proc = git(
        repo,
        "diff",
        "-z",
        "--name-only",
        "--no-renames",
        "--diff-filter=A",
        f"{target}...{branch}",
        step=STEP_UNTRACKED,
        env=env,
    )
    return [item for item in proc.stdout.split("\0") if item]


def guard_untracked_overwrite(repo, target, branch, emit, env=None):
    """第 2 步收尾的预检：这次合并会不会覆盖工作区里的未跟踪文件。

    必须在打回退点、跑临时 worktree 之前——git 自己那句话要等到真 merge 才出现，
    那时前面的预演与测试都白跑了。**只看只报，不删不搬任何用户文件。**
    """
    added = target_new_paths(repo, target, branch, env=env)
    if not added:
        emit("未跟踪文件预检：这次合并没有新增文件，不涉及未跟踪文件。")
        return []
    hits = untracked_overlap(added, untracked_worktree_files(repo, env=env))
    if not hits:
        emit(f"未跟踪文件预检：合并要新增 {len(added)} 个文件，工作区里没有同名未跟踪文件。")
        return []
    detail = "\n".join("    " + path for path in hits)
    raise Fail(
        STEP_UNTRACKED,
        f"这次合并要新增的路径，工作区里已经有同名的未跟踪文件，git merge 会以 "
        f"`untracked working tree file would be overwritten` 拒绝：\n{detail}",
        "把这些文件挪到 .data/ 下（例如 .data/untracked/<工单号>/），把挪动后的路径写进 "
        "submit_result 的 evidence，再跑一次合并。不要挪去系统 Temp：那里会被系统清理，"
        "验收时想核对就找不回来了。内容与分支逐字节相同也一样会被拒，不必去比内容。"
        "本工具只看只报，不会替你删或搬任何文件。",
    )


# --- 第 0 步：合并互斥锁 ---------------------------------------------------
#
# 为什么需要它：两个协调会话同时跑 `auto_merge`，两边都会走过「目标分支没动 → 打回退点
# → 真合」，后一个的回退点会指向已经被改动过的 master，于是「红了回滚」回滚到一半。
# 互斥锁把这件事变成一个确定性的失败：第二次在动任何东西之前就被拒，输出里写着谁占着。
# 锁的位置不能按「当前检出的仓库根」算：主检出的 `.git` 是目录，工作树的 `.git` 是个
# 文件，两者共享同一个 git 公共目录。按检出根算路径时，工作树里放一把活锁，主检出的
# 合并照样能取到自己的锁，两把互不拦（AW-027 验收实测出来的）。
LOCK_NAME = "auto-merge.lock"

# 旧版本把锁放在 `.data/auto-merge.lock`。留着这个名字是为了让升级前留下的锁仍被认出来，
# 而不是被当成普通的未跟踪文件去问人。
LEGACY_LOCK_REL = Path(".data") / LOCK_NAME

# 超过这个时长就当持有者已经不在了。合并最长要跑两次全量测试，两小时足够；
# 阈值不能太短，否则一次慢测试就会让自己的锁被抢走。
LOCK_STALE_SECONDS = 2 * 60 * 60


def git_common_dir(repo, env=None) -> Path:
    """git 公共目录的绝对路径。主检出与它的每个工作树都解析到同一个值。

    不拼字符串猜：`--path-format=absolute` 让 git 自己给出绝对路径（git 2.31+）。
    更老的 git 不认这个选项，就退回到它输出的相对路径——那是相对调用时的 cwd 给的，
    而 cwd 就是检出根，按检出根解析成绝对路径即可。

    git 不存在时立刻说「找不到 git 命令」，不能落到下面那句「不是 git 仓库」上：
    那会把「没装 git」误诊成「目录选错了」，人照着补救句翻目录，永远翻不到。
    """
    repo = Path(repo).resolve()
    for extra in (["--path-format=absolute"], []):
        try:
            proc = subprocess.run(
                ["git", "rev-parse", *extra, "--git-common-dir"],
                cwd=str(repo),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=git_env(env),
            )
        except FileNotFoundError as exc:
            raise git_missing_fail(STEP_LOCK, exc)
        except subprocess.TimeoutExpired:
            raise git_timeout_fail(STEP_LOCK, ["rev-parse", "--git-common-dir"], 600)
        text = (proc.stdout or "").strip()
        if proc.returncode != 0 or not text:
            continue
        candidate = Path(text)
        return (candidate if candidate.is_absolute() else repo / candidate).resolve()
    raise Fail(
        STEP_LOCK,
        f"{repo} 不是 git 仓库，算不出锁该放在哪里。",
        FIX_NOT_A_REPO,
    )


def lock_path(repo, env=None) -> Path:
    """合并锁的绝对路径：落在 git 公共目录里，主检出与任一工作树共用同一把。"""
    return git_common_dir(repo, env=env) / LOCK_NAME


def _iso(stamp: datetime) -> str:
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text):
    """解析锁文件里的时间；解析不了就返回 None，由调用方退回到文件 mtime。"""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _human_span(seconds) -> str:
    seconds = max(int(seconds), 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def _pid_alive(pid) -> bool:
    """这个 pid 还在进程表里吗。

    判错的方向决定后果：说活着 → 多等一会儿，最多少合一次；说死了 → 抢锁，可能两个人
    同时改 master。所以宁可往「活着」那侧错，只有确定不在时才返回 False。
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x102
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            # 打不开：pid 超出范围/已退出；也可能是不允许查询。后者按「不在」处理会
            # 抢锁，但它只会发生在别人的进程上，而合并工具都跑在同一个账号下。
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在，但不是本进程开的
    except (OverflowError, OSError):
        return False
    return True


def read_lock_file(path) -> dict:
    """读锁文件。读不出来或不是对象就当「内容不可辨」，由 inspect_lock 按 mtime 判残留。"""
    try:
        data = json.loads((path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def inspect_lock(path, *, now=None, stale_seconds=LOCK_STALE_SECONDS) -> dict:
    """看这把锁是什么情况：谁拿着、拿了多久、还算不算活着。"""
    path = Path(path)
    info = read_lock_file(path)
    now = now or datetime.now(timezone.utc)
    started = _parse_iso(info.get("started_at"))
    if started is not None:
        started_text = info["started_at"]
    else:
        try:
            started = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            started_text = f"{_iso(started)}（按锁文件修改时间推算）"
        except OSError:
            started = now
            started_text = "(读不到开始时间)"
    elapsed = max((now - started).total_seconds(), 0.0)

    pid = info.get("pid")
    reasons = []
    if pid is None:
        reasons.append("锁文件里没有可用的 pid")
    elif not _pid_alive(pid):
        reasons.append(f"pid {pid} 已不在进程表里")
    if elapsed > stale_seconds:
        reasons.append(f"已持续 {_human_span(elapsed)}，超过 {_human_span(stale_seconds)} 的阈值")

    return {
        "info": info,
        "ticket": info.get("ticket"),
        "pid": pid,
        "started_text": started_text,
        "elapsed": elapsed,
        "stale": bool(reasons),
        "reasons": reasons,
    }


def describe_occupant(state) -> str:
    """占用者的一句话。工单 / pid / 开始时间 / 已持续多久，四样都有才算说得清。"""
    return (
        f"工单 {state['ticket'] or '(未记录工单号)'} / pid "
        f"{state['pid'] if state['pid'] is not None else '(未记录)'} / "
        f"开始于 {state['started_text']}（已持续 {_human_span(state['elapsed'])}）"
    )


def acquire_merge_lock(repo, ticket_id, emit, *, stale_seconds=None, now=None, env=None) -> dict:
    """取合并互斥锁。别人占着且还活着就 Fail；残留锁就显式接管并写明接的是哪把。"""
    path = lock_path(repo, env=env)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise lock_unwritable_fail(path.parent, exc)
    stale_seconds = LOCK_STALE_SECONDS if stale_seconds is None else stale_seconds
    now = now or datetime.now(timezone.utc)
    payload = {"pid": os.getpid(), "ticket": ticket_id, "started_at": _iso(now)}
    took_over = []

    # O_EXCL 建文件是原子的：谁建成谁持锁，不需要另做比对。接管残留锁走「删了重建」，
    # 中间可能被第三者插队，所以重试几次，而不是删完就假定自己拿到了。
    for _ in range(5):
        try:
            handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            state = inspect_lock(path, now=now, stale_seconds=stale_seconds)
            occupant = describe_occupant(state)
            if not state["stale"]:
                raise Fail(
                    STEP_LOCK,
                    f"另一次合并正在跑：{occupant}。锁文件：{path.as_posix()}",
                    "等它结束再跑（正常结束或出错都会自己释放）。要看它在合哪张单，就看上面"
                    f"的工单号，或读 {path.as_posix()}；不要手工删锁——进程被强杀留下的锁会"
                    "被下一次运行自动接管，而删掉一把活着的锁会让两次合并同时改目标分支。",
                )
            emit(f"接管残留锁：{occupant}（{'；'.join(state['reasons'])}）。")
            took_over.append(occupant)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise lock_unwritable_fail(path, exc)
            continue
        except OSError as exc:
            # 目录在但写不进去：只读检出、权限、磁盘只读，都落在这里。
            raise lock_unwritable_fail(path, exc)
        else:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            emit(
                f"合并互斥：取到锁 {path.as_posix()}（工单 {ticket_id}，"
                f"pid {payload['pid']}，开始于 {payload['started_at']}）。"
            )
            return {"path": path, "pid": payload["pid"], "ticket": ticket_id,
                    "started_at": payload["started_at"], "took_over": took_over}

    raise Fail(
        STEP_LOCK,
        "连续 5 次都没拿到锁：有别的进程在抢同一把锁。",
        f"等几秒再跑；一直这样就去看 {path.as_posix()} 里写的是哪个工单。",
    )


def lock_unwritable_fail(target, exc) -> Fail:
    """公共目录不可写：锁建不出来，合并就不能开始——要说清是哪条路径写不了。"""
    return Fail(
        STEP_LOCK,
        f"合并锁写不进去：{Path(target).as_posix()}（{exc}）。",
        "确认这个目录可写（它在仓库的 git 公共目录里，平时是 <仓库>/.git）；"
        "只读检出、权限不足或盘满都会这样，别把锁改到别处绕过去——那会让两次合并同时改目标分支。",
    )


def release_merge_lock(lock, emit) -> None:
    """释放锁。只删自己那把——被接管过的旧持有者不许把新持有者的锁一起删掉。"""
    path = Path(lock["path"])
    info = read_lock_file(path)
    if info.get("pid") != lock["pid"]:
        emit(f"互斥锁已不属于本进程（文件里写的是 pid {info.get('pid')}），不删。")
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        emit(f"互斥锁没删掉（{exc}）；下次合并会把它当残留锁接管。")


def is_lock_artifact(path: str, repo=None) -> bool:
    """锁文件是本工具自己的产物，不该出现在「未跟踪文件」清单里。

    锁现在放在 git 公共目录里，`git status` 本来就不会列出它；旧版本放在 `.data/` 下，
    升级后可能还留着一把，这里一并认出来。
    """
    normalized = path.replace("\\", "/").strip().rstrip("/")
    if normalized in (LEGACY_LOCK_REL.as_posix(), LEGACY_LOCK_REL.parent.as_posix()):
        return True
    if repo is None:
        return False
    try:
        relative = lock_path(repo).relative_to(Path(repo).resolve()).as_posix()
    except (ValueError, OSError, Fail):
        return False
    return normalized in {relative, relative.rsplit("/", 1)[0]}


def run_merge(
    repo,
    ticket_id,
    *,
    branch=None,
    target=None,
    dry_run=False,
    record_only=False,
    ticket_reader=None,
    merge_recorder=None,
    out=None,
    env=None,
    python=None,
    db_path=None,
):
    """整个合并流程。返回 0 表示成功；失败抛 Fail，由 main 转成非零退出。"""
    emit = out or print
    repo = Path(repo).resolve()
    branch = branch or f"ticket/{ticket_id}"
    python = python or sys.executable

    # 第 2 步的配置判在拿锁与读工单之前：本次合并到底该跑什么、合进哪里，
    # 都写在目标项目的 .agents/ticket-config.json 里。缺配置时一步都不动，
    # 而不是先拿锁、跑一遍默认命令、再在记录那一步发现没配测试。
    config = load_project_config(repo)
    target = target or config["target_branch"]

    if ticket_reader is None or merge_recorder is None:
        center, project_id = open_center_for_project(repo, db_path, STEP_TICKET)
        if ticket_reader is None:
            ticket_reader = center_ticket_reader(center, project_id)
        if merge_recorder is None:
            merge_recorder = center_merge_recorder(center)

    # 第 0 步：互斥锁。必须在第 1 步之前拿——否则拿不到锁时撞上的是「这单状态不对」
    # 之类与真实原因无关的报错，而真正要说的那句话是「谁占着」。
    lock = acquire_merge_lock(repo, ticket_id, emit, env=env)
    try:
        emit(f"项目配置：{config['path']}（目标分支 {target}，测试 {' '.join(config['test_cmd'])}）")
        if record_only:
            # 半截状态的收口路径：同一把锁、同一道工单门，只是判据反过来问。
            return _record_only_steps(
                repo,
                ticket_id,
                branch=branch,
                target=target,
                ticket_reader=ticket_reader,
                merge_recorder=merge_recorder,
                emit=emit,
                env=env,
                python=python,
                config=config,
            )
        return _merge_steps(
            repo,
            ticket_id,
            branch=branch,
            target=target,
            dry_run=dry_run,
            ticket_reader=ticket_reader,
            merge_recorder=merge_recorder,
            emit=emit,
            env=env,
            python=python,
            config=config,
        )
    finally:
        release_merge_lock(lock, emit)


def _step_ticket(ticket_id, ticket_reader, emit) -> dict:
    """第 1 步：工单本身必须有资格合并。返回工单详情，后面写记录要用 project_id。

    正常合并与 `--record-only` 共用这一段：两边都必须 done、都没有合并记录。
    已有记录时报的是库里的错误码 `ALREADY_MERGED`，因为人会在两个地方看到同一句话：
    工具输出与 `merge_ticket` 的回执。
    """
    ticket = ticket_reader(ticket_id)
    state = ticket.get("state")
    if state != "done":
        raise Fail(
            STEP_TICKET,
            f"工单 {ticket_id} 当前 state={state}，只有验收通过（done）的工单可以合并。",
            "先让独立验收会话 review_result 通过；返工中（rework）或执行中的工单不要合并。",
        )
    merges = ticket.get("merges") or []
    if merges:
        first = merges[0]
        # merges 行的字段叫 commit_sha（见 task_center/db.py 的建表语句与 get_ticket 的
        # SELECT *）。读 .get("commit") 拿到的是 None，提示就会印成「commit None」——
        # 看着像合并记录里没有提交号，其实是读错了键（AW-024）。
        raise Fail(
            STEP_TICKET,
            f"工单 {ticket_id} 已有合并记录（ALREADY_MERGED：commit {first.get('commit_sha')} → "
            f"{first.get('target_branch')}）。",
            "已经合并过了，不要再合一次；要核对就读 get_ticket 的 merges。",
        )
    emit(f"工单：{ticket_id}  {ticket.get('title', '')}  state=done")
    return ticket


def notice_precheck(ticket, ticket_id, emit, dry_run=False) -> None:
    """第 1 步收尾：没过回执的单，一步都不许动。

    库里 `merge_ticket` 本来就有这道门（`NO_NOTICE`），但它在第 7 步。AW-061 的
    验收实测过那种现场：`git merge` 已经落到 master（前进了一个提交）、`merges` 里
    没有记录、journal 停在 `tests-passed`；照提示跑 `--record-only` 撞的是同一句
    `NO_NOTICE`，提示自指。所以要提前到任何写入之前——被拒时 master、回退点 tag、
    journal、临时 worktree、工作树与分支五样一样都没动。

    `failed` 也算走过：回执没送达但原因记下来了，这是事实，不是能拿来越过的门。
    所以这里只数行数、不看 status，与库里那道判的判据完全一致（`notices` 一行都没有）。

    `dry_run=True` 时只输出一条警告就放行：预演不写任何东西，它的价值就在于**能把
    整套预演走完**（冲突预演 + 临时 worktree 里的全量测试）。以前这里不分路径地拒，
    结果「预演」被一道与预演无关的门提早就地终止了（AW-062 实测）。真合并仍然过不去：
    不加 `--dry-run` 的时候这句照旧 `Fail`。

    只走正常合并路径：`--record-only` 的适用场景本来就是「合并已经发生、只差记录」，
    多一道前置只会让恢复更绕，那种现场仍由 `merge_ticket` 拒绝。
    """
    notices = ticket.get("notices") or []
    if notices:
        latest = notices[-1]
        emit(
            f"回执：{len(notices)} 条，最后一条 status={latest.get('status') or '(未记)'}。"
        )
        return
    if dry_run:
        emit(
            f"警告：工单 {ticket_id} 没有任何回执记录（NO_NOTICE）。--dry-run 只预演不写，"
            "照常往下跑；真要合并会被同一道判在第 1 步挡住——先补一条回执"
            f"（`python tools/notice_payload.py --ticket {ticket_id}` → `send_to_session` 带 "
            "`target_session_id` → `record_notice`；送不到就记 `failed` 并写清原因）。"
        )
        return
    raise Fail(
        STEP_NOTICE,
        f"工单 {ticket_id} 没有任何回执记录（NO_NOTICE），合并不进去。",
        f"先跑 `python tools/notice_payload.py --ticket {ticket_id}` 取回执参数，把回执"
        "发回协调会话（`send_to_session` 必须带 `target_session_id`）再 `record_notice` "
        "落库；送不到就记一条 `failed` 并在 detail 里写清原因——`failed` 也算走过回执。"
        "这道判在打回退点、写 journal、建临时 worktree 之前，所以此刻 master、tag、"
        "journal、工作树与分支都没有动过，补完回执直接重跑即可，不需要 --record-only。",
    )


def _step_checks(repo, target, branch, env, emit, *, require_ahead=True) -> dict:
    """第 2 步：前置检查，全部在动目标分支之前完成。

    `require_ahead=True` 是正常合并：源分支必须**领先**目标，空合并不做。
    `require_ahead=False` 是 `--record-only`：源分支必须**已经全在目标里**——
    半截合并的现场正好是「领先 0 个提交」，与正常合并的判据正好相反。

    返回 {"base": 目标当前提交, "tip": 源分支 tip, "ahead": 领先数, "untracked": [...]}。
    未跟踪文件与「合并要新增的路径」的预检不在这里做：那是**要写工作区**的合并才有的
    动作，`--record-only` 一个文件都不写，没必要也拦不到东西。
    """
    if git(repo, "rev-parse", "--git-dir", step=STEP_CHECKS, check=False, env=env).returncode != 0:
        raise Fail(STEP_CHECKS, f"{repo} 不是 git 仓库。", "在仓库根跑；--repo 指向含 .git 的目录。")

    # 最先检查「检出在目标分支上」：这是**方向**问题，比脏工作区更严重。
    # 主检出停在 ticket/AW-0xx 时跑合并，`git merge --no-ff <源分支>` 会把 master
    # 合进那条工单分支：方向反了，两个分支都被改，而且看起来像一次成功合并。
    # 放在最前面，是为了让报错说中原因，而不是被“未提交改动”之类的问题挡住。
    current = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", step=STEP_CHECKS, check=False, env=env)
    current = current.stdout.strip()
    if current != target:
        raise Fail(
            STEP_CHECKS,
            f"当前检出在 {current or '游离 HEAD'} 上，不是目标分支 {target}。合并方向会反。",
            f"先 git switch {target} 再跑一次；{target} 与 {current or '当前分支'} 的 commit 都没有变动。",
        )

    status = git(repo, "status", "--porcelain", step=STEP_CHECKS, env=env).stdout
    tracked_changes, untracked, _ignored = split_status(status)
    untracked = [path for path in untracked if not is_lock_artifact(path, repo)]
    if tracked_changes:
        detail = "\n".join("    " + line for line in tracked_changes)
        raise Fail(
            STEP_CHECKS,
            f"当前检出有已跟踪文件的未提交改动：\n{detail}",
            "先提交或收起来（git stash -u）再合并；合并工具不会替你丢掉改动。"
            "只有未跟踪文件不拦合并，但也不参与合并。",
        )
    if untracked:
        # 不拦，但要说清：这些文件留在工作区，没有进这次合并。
        emit(f"注意：检出里有 {len(untracked)} 个未跟踪文件，它们没有被合进去：")
        for path in untracked:
            emit(f"    {path}")
        emit("       （未跟踪文件不参与合并，所以不影响结果；要纳入就先提交在源分支上。")
        emit("        但若其中有路径正好是这次合并要新增的文件，下一步预检会拦下：")
        emit("        git 不肯覆盖未跟踪文件，内容逐字节相同也不肯。）")

    if not exists(repo, f"refs/heads/{target}", env):
        raise Fail(
            STEP_CHECKS,
            f"目标分支 {target} 不存在。",
            "核对 --target；默认是 master。",
        )
    if not exists(repo, f"refs/heads/{branch}", env):
        raise Fail(
            STEP_CHECKS,
            f"源分支 {branch} 不存在。",
            f"先用 --branch 指定真实分支；约定是 ticket/{ticket_id}。",
        )

    base = git_out(repo, "rev-parse", target, step=STEP_CHECKS, env=env)
    tip = git_out(repo, "rev-parse", f"refs/heads/{branch}", step=STEP_CHECKS, env=env)
    ahead = int(git_out(repo, "rev-list", "--count", f"{target}..{branch}", step=STEP_CHECKS, env=env) or "0")

    if require_ahead:
        if ahead == 0:
            raise Fail(
                STEP_CHECKS,
                f"{branch} 没有领先 {target} 的提交（rev-list --count {target}..{branch} = 0）。",
                "确认改动已提交在源分支上；空合并不做。"
                "若合并**已经成功、只差写记录**，用 --record-only 收口：它把这道判据反过来问。",
            )
        clean_note = "已跟踪文件无改动（未跟踪文件不参与合并）" if untracked else "检出干净"
        emit(f"检查：{clean_note}、{target} 在 {base[:12]}、{branch} 领先 {ahead} 个提交。")
    else:
        # 反向判据：源分支必须已经全部在目标里。两个都查——is-ancestor 认的是「能走到」，
        # rev-list --count 是同一个事实的可读数字，两个都说成立才不算误判。
        ancestor = git(
            repo, "merge-base", "--is-ancestor", branch, target,
            step=STEP_CHECKS, check=False, env=env,
        )
        if ancestor.returncode != 0 or ahead != 0:
            raise Fail(
                STEP_CHECKS,
                f"{branch} 还不能算「已经全在 {target} 里」：git merge-base --is-ancestor 退出 "
                f"{ancestor.returncode}，rev-list --count {target}..{branch} = {ahead}。",
                "--record-only 只用于「合并已经成功、记录还没写」的现场。还想再改就在分支上继续"
                "提交，然后走正常合并；不要用它替一次真实合并签字。",
            )
        emit(
            f"检查：{target} 已包含 {branch} 的全部提交（is-ancestor 成立、领先 {ahead} 个提交），"
            f"{target} 在 {base[:12]}。"
        )

    return {"base": base, "tip": tip, "ahead": ahead, "untracked": untracked}


# --- 被验收的树与要合的树 -------------------------------------------------
#
# 这一道比较以前不存在：`merges` 只记「合到了哪个提交」，不记「验收的是哪棵树」。于是验收
# 之后又往分支上提一次，合并照样通过，而库里查不出来——R-008 那次合并两棵树刚好相同，
# 是因为验收后没人再提交，不是因为闸门查过。
#
# 比对的两侧分属两个世界，所以只能在这里做：git 侧是分支 tip（`git rev-parse`），库侧是
# 提交时写下的 `submissions.head_sha`。`task_center` 不跑 git，它只做库内比对。

UNVERIFIED = "unverified"


def reviewed_head_sha(ticket) -> str | None:
    """工单最新一次提交记下的 head sha；旧数据或没有提交时返回 None。

    `get_ticket` 的 `submissions` 按写入顺序返回，所以「最新」是最后一条。
    """
    submissions = ticket.get("submissions") or []
    if not submissions:
        return None
    return (submissions[-1] or {}).get("head_sha")


def guard_reviewed_sha(ticket, ticket_id, branch, tip, emit) -> str:
    """比对「被验收的 sha」与「现在要合的 tip」，返回要交给 `merge_ticket` 的 reviewed_sha。

    三种情形都对得上，因为它们说的是三件不同的事：

    - 相等：合的就是验收过的那棵树，写那个 sha。
    - 不等：验收之后分支又动了。这是**一步不动**的失败：master、回退点、journal、
      临时工作树与分支一样都没动，因为这一步在它们之前。
    - 库里是 NULL（schema 8 以前的老提交）：没有可比的值，不阻止合并，但也不静默放行——
      输出里明说这单没记录 sha，合并记录按 `unverified` 落库，于是事后查得到它没核过。
    """
    head = reviewed_head_sha(ticket)
    if head is None:
        emit(
            f"警告：工单 {ticket_id} 最新一次提交没有记录 head sha（schema 9 之前的提交），"
            f"没有可比的值。这次照常合并，合并记录按 `{UNVERIFIED}` 落库——"
            "事后查得到它没被核对过，而不是被当成「核对过」。"
        )
        return UNVERIFIED
    head = str(head).casefold()
    if head != tip.casefold():
        raise Fail(
            STEP_SHA,
            f"被验收的 {head} 与现在要合的 {tip} 不是同一个提交：验收之后 {branch} 又变动过了。",
            f"要让这一次合并成立，得让被验收的就是要合的那棵树：要么把 {branch} 退回被验收的"
            f"那个提交（{head[:12]}）再合，要么让独立验收会话对现在的 tip 重新验收一次"
            f"（提交一次带 head_sha 的成果，再 review_result）。此刻 {ticket_id} 的合并记录没写、"
            "回退点 tag 没打、临时工作树没建，目标分支一指头没动。",
        )
    emit(f"被验收的树：{head}（与 {branch} 的 tip 一致）。")
    return head


# --- --record-only：收口「合并已成功、记录还没写」的半截状态 -----------------
#
# AW-058 断在这里：`git merge` 已经成功、master 上已经有那次合并提交，`merges` 里没有
# 这一行，公共目录里留着一把 pid 早已不在的锁，工作树与分支都还在。默认路径在这种现场
# 一步都走不动——第 2 步要「源分支领先目标」，而领先数正好是 0。所以这条路径把判据
# 反过来问，并把剩下的两件事（跑测试、写记录）重新走一遍，然后才清理。


def find_merge_commit(repo, target, source_tip, env=None):
    """在目标的祖先里找「parent2 正好是源分支 tip」的那次合并提交。

    为什么不直接取 HEAD：半截状态里目标分支可能已经前进过（合并后又提交了别的），
    记录里要写的是**那次合并本身**。也不按说明里的工单号找——说明是人写的；这里要的
    是 git 自己的结构事实：`--no-ff` 合并提交的第二个父提交就是被合进来的那一支。
    找不到（快进合并没有合并提交）就返回 None，由调用方拒绝。
    """
    log = git(repo, "rev-list", "--parents", target, step=STEP_FIND, env=env).stdout
    for line in log.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == source_tip:
            return parts[0]
    return None


# 合并进程记录：`.data/merge-journal/<工单号>.json`。
#
# 为什么要有它：一次真实合并在「合并提交已经落到工作区」与「记录已经写进库」之间
# 隔着两场全量测试，进程被杀就停在那一段，事后只能靠锁文件与 git log 推。写一份
# 小小的步骤记录，事后 `cat` 一下就知道当时做到哪一步。它是诊断品，不是签字：
# 写不进去只报告，不拦合并（磁盘只读或 .data 被删，不该把合并挡住）。
JOURNAL_DIR = Path(".data") / "merge-journal"


def journal_path(repo, ticket_id) -> Path:
    """合并进程记录的路径。纯拼路径，不建目录。"""
    return Path(repo) / JOURNAL_DIR / f"{ticket_id}.json"


def write_journal(repo, ticket_id, payload, emit=None) -> bool:
    """写/刷新合并进程记录。失败只报告并返回 False，不抛 Fail。"""
    path = journal_path(repo, ticket_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        if emit:
            emit(
                f"注意：合并进程记录写不进去（{path.as_posix()}：{exc}）；"
                "进程被杀之后看不出当时做到第几步，但合并照常继续。"
            )
        return False
    if emit:
        emit(f"合并进程记录：{path.as_posix()}（step={payload.get('step')}）")
    return True


def clear_journal(repo, ticket_id, emit=None, mark=None) -> None:
    """删掉合并进程记录。`mark` 给了就先把它改写成那个 step 再删（回滚时留下的那句话）。"""
    path = journal_path(repo, ticket_id)
    if mark is not None and path.is_file():
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        if not isinstance(info, dict):
            info = {}
        info["step"] = mark
        write_journal(repo, ticket_id, info, emit=None)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        if emit:
            emit(f"合并进程记录没删掉（{path.as_posix()}：{exc}）；不影响结果，下次合并会覆盖。")
        return
    if emit:
        emit(f"合并进程记录已清：{path.as_posix()}")


def cleanup_after_record(repo, ticket_id, branch, emit, env=None) -> list[str]:
    """记录写成功之后清理工作树与分支。返回没做成的事（空列表就是都做成了）。

    清理失败**不改退出码**：那时该做的是人工收拾，不是重跑（重跑会被第 1 步的
    `ALREADY_MERGED` 挡回来）。工具也不 `--force` 删工作树——里面有未提交改动时
    `git worktree remove` 会拒绝，那正是该保留的东西；硬删等于替人丢文件。
    """
    problems: list[str] = []
    worktree = Path(repo).resolve() / ".worktrees" / ticket_id
    listed = []
    proc = git(repo, "worktree", "list", "--porcelain", step=STEP_CLEANUP, check=False, env=env)
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            listed.append(Path(line[len("worktree "):].strip()))
    registered = any(_same_path(path, worktree) for path in listed)
    if registered:
        removed = git(repo, "worktree", "remove", str(worktree), step=STEP_CLEANUP, check=False, env=env)
        if removed.returncode != 0:
            problems.append(
                f"git worktree remove {worktree.as_posix()} 失败："
                f"{(removed.stderr or removed.stdout).strip()}"
                "（多半是那里还有未提交改动——先看一遍再自己决定怎么收）"
            )
        else:
            emit(f"清理：已删除工作树 {worktree.as_posix()}")
    elif worktree.exists():
        problems.append(
            f"{worktree.as_posix()} 目录还在，但它不在 git worktree 清单里；"
            "本工具不替你删这个目录"
        )
    else:
        emit(f"清理：工作树 .worktrees/{ticket_id} 不存在，跳过。")

    if exists(repo, f"refs/heads/{branch}", env):
        deleted = git(repo, "branch", "-d", branch, step=STEP_CLEANUP, check=False, env=env)
        if deleted.returncode != 0:
            problems.append(
                f"git branch -d {branch} 失败：{(deleted.stderr or deleted.stdout).strip()}"
                "（分支还没合进目标时 git 会拒绝，那说明记录与 git 那边不一致，先核对再动）"
            )
        else:
            emit(f"清理：已删除分支 {branch}")
    else:
        emit(f"清理：分支 {branch} 不存在，跳过。")

    for problem in problems:
        emit(f"清理未完成：{problem}")
    if problems:
        emit("清理要靠人了：合并记录已经写好，不要重跑（重跑会以 ALREADY_MERGED 被拒）。")
    return problems


def _same_path(left, right) -> bool:
    """两个路径是不是同一个（Windows 不分大小写，分隔符也不分）。只用于比对。"""
    try:
        return normalize_git_path(str(Path(left).resolve())) == normalize_git_path(str(Path(right).resolve()))
    except OSError:
        return normalize_git_path(str(left)) == normalize_git_path(str(right))


def _record_only_steps(
    repo,
    ticket_id,
    *,
    branch,
    target,
    ticket_reader,
    merge_recorder,
    emit,
    env,
    python,
    config,
):
    """`--record-only`：已合并且无记录的现场，跑测试 → 写记录 → 清理。锁由 run_merge 管。"""
    ticket = _step_ticket(ticket_id, ticket_reader, emit)
    emit(f"分支：{branch} → {target}（--record-only：不碰目标分支）")

    checks = _step_checks(repo, target, branch, env, emit, require_ahead=False)
    tip = checks["tip"]

    # 同一道比对：`--record-only` 比的是源分支 tip，也就是那次合并合进来的那一支。
    reviewed_sha = guard_reviewed_sha(ticket, ticket_id, branch, tip, emit)

    commit = find_merge_commit(repo, target, tip, env=env)
    if commit is None:
        raise Fail(
            STEP_FIND,
            f"{target} 的祖先里找不到「parent2 == {branch} 的 tip（{tip[:12]}）」的合并提交。",
            "这条路径只收「--no-ff 真合过、但记录没写」的现场。快进合并没有合并提交，"
            "记录下来会指错提交，所以不写：先核对 git log --graph，再由人决定这张单怎么算。",
        )
    emit(f"合并提交：{commit[:12]}（{target} 的祖先；parent2 是 {branch} 的 tip {tip[:12]}）")

    passed, summary, output = run_tests_in(
        repo, python=python, test_cmd=config["test_cmd"], env=env,
        timeout=config["test_timeout"], step=STEP_RECORD_TESTS,
    )
    emit(output.rstrip())
    if not passed:
        tag = f"pre-merge-{ticket_id}"
        fallback = f"{tag}（回退点）" if exists(repo, f"refs/tags/{tag}", env) else f"{commit}^1（合并前的 {target}）"
        raise Fail(
            STEP_RECORD_TESTS,
            f"全量测试失败（{summary}）。没有写合并记录，也没有清理工作树与分支。",
            f"先修测试再重跑。回退点：{fallback}；要退就 git reset --hard 它，别重跑合并。",
        )
    emit(f"记录前测试通过：{summary}")

    payload = {
        "project_id": ticket.get("project_id") or "",
        "ticket_id": ticket_id,
        "commit": commit,
        "target_branch": target,
        "tests": summary,
        "reviewed_sha": reviewed_sha,
    }
    receipt = merge_recorder(payload)
    emit(f"合并记录：{json.dumps(receipt, ensure_ascii=False)}")

    cleanup_after_record(repo, ticket_id, branch, emit, env=env)
    emit(f"完成：{ticket_id} 的合并记录已补上（{commit[:12]} → {target}）。")
    return 0


def _merge_steps(
    repo,
    ticket_id,
    *,
    branch,
    target,
    dry_run,
    ticket_reader,
    merge_recorder,
    emit,
    env,
    python,
    config,
):
    """第 1~7 步。锁由 run_merge 取与释放，所以这里不用管锁。"""
    # 第 1 步：工单本身必须有资格合并
    ticket = _step_ticket(ticket_id, ticket_reader, emit)
    emit(f"分支：{branch} → {target}")

    # 第 1 步收尾：回执。库里 merge_ticket 也判这一条，但那时合并已经落进目标分支了。
    # 放在这里而不是更后面，是为了让「没走过回执」变成一个一步都不动的失败。
    # dry-run 只降为警告：预演要能预演到底，见 notice_precheck 的说明。
    notice_precheck(ticket, ticket_id, emit, dry_run=dry_run)

    # 第 2 步：前置检查，全部在动目标分支之前完成
    checks = _step_checks(repo, target, branch, env, emit)
    base = checks["base"]

    # 第 2 步收尾之二：被验收的树是不是要合的那棵树。
    # 放在打回退点与预演之前——它只读库与一个 ref，拒的时候什举都没动，
    # 而不是先花一场全量测试再在真合并前失败。
    reviewed_sha = guard_reviewed_sha(ticket, ticket_id, branch, checks["tip"], emit)

    # 第 2 步收尾：合并要写进工作区的新路径，有没有撞上未跟踪文件。
    # 放在打回退点与预演之前，是为了让这句失败不代价一场全量测试；
    # 放在目标/源分支存在性检查之后，是因为它要用两个 ref 算 merge-base。
    guard_untracked_overwrite(repo, target, branch, emit, env=env)

    # 第 3 步：冲突预演（不写任何东西）
    preview_conflict(repo, target, branch, emit, env=env)

    # 第 4 步：回退点。dry-run 不建，免得占掉真实合并要用的名字。
    tag = f"pre-merge-{ticket_id}"
    if dry_run:
        emit(f"第 4 步 · dry-run：不建回退点 {tag}，也不碰 {target}。")
    else:
        if exists(repo, f"refs/tags/{tag}", env):
            raise Fail(
                STEP_TAG,
                f"回退点 {tag} 已存在（指向 {git_out(repo, 'rev-list', '-n', '1', tag, step=STEP_TAG, env=env)[:12]}）。",
                "说明之前合并或回退过这张工单。先核对 git log 与 git tag，不要覆盖旧回退点；"
                "确需重新合并就由人决定改名或删掉旧 tag。",
            )
        git(repo, "tag", tag, target, step=STEP_TAG, env=env)
        emit(f"回退点：{tag} → {target}@{base[:12]}")

    # 第 5 步：临时 worktree 里先合一次并跑测试。失败时目标分支一动未动。
    tmp_root = Path(tempfile.mkdtemp(prefix=f"auto-merge-{ticket_id}-"))
    worktree = tmp_root / "wt"
    summary = ""
    try:
        git(repo, "worktree", "add", "--detach", str(worktree), target, step=STEP_PREVIEW, env=env)
        merged = git(
            worktree, "merge", "--no-ff", branch,
            "-m", f"auto_merge 预演：{branch} → {target}（{ticket_id}）",
            step=STEP_PREVIEW, check=False, env=env,
        )
        if merged.returncode != 0:
            conflicted = git(
                worktree, "diff", "--name-only", "--diff-filter=U",
                step=STEP_PREVIEW, check=False, env=env,
            ).stdout.split()
            git(worktree, "merge", "--abort", step=STEP_PREVIEW, check=False, env=env)
            raise Fail(
                STEP_PREVIEW,
                f"临时 worktree 里合并失败（冲突：{', '.join(conflicted) or '未列出文件'}）。",
                "先在源分支上解冲突并提交再重跑；目标分支没有被改动。",
            )

        passed, summary, output = run_tests_in(
            worktree, python=python, test_cmd=config["test_cmd"], env=env,
            timeout=config["test_timeout"], step=STEP_PREVIEW,
        )
        emit(output.rstrip())
        if not passed:
            raise Fail(
                STEP_PREVIEW,
                f"临时 worktree 里全量测试失败（{summary}）。目标分支一动未动。",
                "先修测试再重跑；这一步失败不会留下合并提交，也不需要回滚。",
            )
        emit(f"预演通过：{summary}")
    finally:
        git(repo, "worktree", "remove", "--force", str(worktree), step=STEP_PREVIEW, check=False, env=env)
        git(repo, "worktree", "prune", step=STEP_PREVIEW, check=False, env=env)
        shutil.rmtree(tmp_root, ignore_errors=True)

    if dry_run:
        emit(f"dry-run：预演通过。{target} 未动（仍在 {base[:12]}），未写合并记录。")
        return 0

    # 第 6 步：真实合并，合并后再跑一次；红了回滚到 tag
    merged = git(
        repo, "merge", "--no-ff", branch,
        "-m", f"Merge {branch} into {target}（工单 {ticket_id}）",
        step=STEP_MERGE, check=False, env=env,
    )
    if merged.returncode != 0:
        git(repo, "merge", "--abort", step=STEP_MERGE, check=False, env=env)
        raise Fail(
            STEP_MERGE,
            f"真实合并失败：{(merged.stderr or merged.stdout).strip()}",
            f"已中止合并，{target} 仍在 {base[:12]}。先解冲突再重跑。",
        )
    commit = git_out(repo, "rev-parse", "HEAD", step=STEP_MERGE, env=env)
    emit(f"合并提交：{commit[:12]}")

    # 合并提交已经落到工作区、记录还没写：这是最容易断的一段（两场全量测试在中间）。
    # 先写一份进程记录，事后 `cat` 一下就知道当时做到第几步。
    journal = {
        "ticket_id": ticket_id,
        "commit": commit,
        "target_branch": target,
        "source_branch": branch,
        "tag": tag,
        "started_at": _iso(datetime.now(timezone.utc)),
        "step": "merged",
    }
    write_journal(repo, ticket_id, journal, emit)

    passed, summary, output = run_tests_in(
        repo, python=python, test_cmd=config["test_cmd"], env=env,
        timeout=config["test_timeout"], step=STEP_MERGE,
    )
    emit(output.rstrip())
    if not passed:
        git(repo, "reset", "--hard", tag, step=STEP_MERGE, env=env)
        # 删前把 step 改成 rolled-back：进程被别的方式杀掉时，人看到的最后一句是「回滚过」。
        clear_journal(repo, ticket_id, emit, mark="rolled-back")
        raise Fail(
            STEP_MERGE,
            f"合并后全量测试失败（{summary}）；已回滚到 {tag}。",
            f"{target} 已回到合并前 {base[:12]}，回退点 {tag} 仍在。先修测试再重跑。",
        )
    emit(f"合并后测试通过：{summary}")
    journal["step"] = "tests-passed"
    write_journal(repo, ticket_id, journal, emit)

    # 第 7 步：把合并写成一条可查询的记录
    payload = {
        "project_id": ticket.get("project_id") or "",
        "ticket_id": ticket_id,
        "commit": commit,
        "target_branch": target,
        "tests": summary,
        "reviewed_sha": reviewed_sha,
    }
    receipt = merge_recorder(payload)
    emit(f"合并记录：{json.dumps(receipt, ensure_ascii=False)}")
    clear_journal(repo, ticket_id, emit)
    emit(f"完成：{ticket_id} 已合入 {target}（{commit[:12]}）。")
    return 0


def main(argv, out=None):
    # Windows 控制台默认 cp936，中文输出重定向到文件会乱码。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    emit = out or print
    parser = argparse.ArgumentParser(description="验收通过后自动合并，失败自动回滚")
    parser.add_argument("--ticket", default=os.environ.get("TASK_CENTER_TICKET"))
    parser.add_argument("--branch", default=None, help="源分支，默认 ticket/<ticket>")
    parser.add_argument("--target", default=None, help="目标分支，默认取项目配置的 target_branch")
    parser.add_argument("--dry-run", action="store_true", help="跑到预演与测试就停，不碰目标分支")
    parser.add_argument(
        "--record-only",
        action="store_true",
        help="已合并且无记录时收口：反向判据 → 跑全量测试 → 补合并记录 → 清理工作树与分支",
    )
    parser.add_argument("--list", action="store_true", help="列出已验收但未合并的工单")
    parser.add_argument("--repo", default=str(REPO))
    parser.add_argument("--db", default=None, help="中心库路径，默认按 center.json 发现")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    try:
        if args.list:
            center, _ = open_center(args.db, STEP_TICKET)
            list_unmerged_rows(center, emit)
            return 0
        if not args.ticket:
            emit("没有工单号。用法：python tools/auto_merge.py --ticket AW-xxx")
            return 2
        if args.dry_run and args.record_only:
            emit("--dry-run 与 --record-only 不能同时用：一个是「只预演不写」，一个是「不预演只写」。")
            return 2
        return run_merge(
            repo,
            args.ticket,
            branch=args.branch,
            target=args.target,
            dry_run=args.dry_run,
            record_only=args.record_only,
            out=emit,
            db_path=args.db,
        )
    except Fail as exc:
        emit(f"合并中止 · {exc.step}")
        emit(f"原因：{exc.reason}")
        if exc.fix:
            emit(f"怎么补：{exc.fix}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
