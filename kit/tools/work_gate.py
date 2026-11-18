"""工单闸门：回答「这次改动凭什么可以落盘」。

前两轮的真实问题不是没人知道该开工单，而是**没有一步会因此失败**：计划文件过了
闸门，代码提交了，中心库里的工单数仍然是 0。这个脚本把「有没有工单、有没有绑定
到本会话的活跃尝试」变成一个退出码，并被两个仓库的 commit 钩子调用。

用法：
    python tools/work_gate.py --ticket T-1              # 通过则退出 0
    python tools/work_gate.py --ticket T-1 --repo {{WORKSPACE}}
    python tools/work_gate.py --list                     # 现在有哪些活跃尝试（读不了库则非零退出）
    python tools/work_gate.py --ticket T-1 --json

钩子里的约定：`TASK_CENTER_TICKET` 给出工单号，`TASK_CENTER_PROJECT` 可选。
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG = Path.home() / ".agents" / "center.json"

# 收尾提交（例外路径）只放开这三个位置。多一条路径就是多一扇门，将来要扩就显式改这里。
CLOSING_EXACT_PATHS = ("tasks/round.md", "tasks/rounds.md")
CLOSING_PACKET = re.compile(r"^tasks/closing-[^/]+\.md$")
# round.md 在收尾提交里只许改状态行：逐行看增删，不按行数糊过去。
ROUND_CHANGED_LINE = re.compile(r"^[-+](?![-+][-+])")
ROUND_STATUS_ANYWHERE = re.compile(r"状态[：:]")
ROUND_NUMBER_LINE = re.compile(r"^轮次[：:]\s*(R-\d{3})\s*$", re.M)
ROUND_STATUS_LINE = re.compile(r"^状态[：:]\s*(\S+)\s*$", re.M)
# 状态位是三档之一时这一轮已经有结论；只有「进行中」才可能是没收。
ROUND_CLOSED_STATUSES = ("已收尾", "等一句答复", "需重开")
# 收尾提交要求「本项目一张在飞的单都没有」：先把还没开始的 ready/rework 也算上。
CLOSING_BLOCKING_STATES = ("dispatching", "running", "awaiting_review", "blocked", "ready", "rework")


def center_candidates(repo=REPO, env=None):
    """按优先级列出可能存放 task_center 代码的目录。

    顺序是「显式指定 → 仓库根 → 工作树」。仓库根排在工作树前面，是因为代码已经
    合回 master（AW-011），工作树只是一份可能落后的检出。反过来的话，工作树里的
    task_center 一旦和 master 不同版本，闸门会**静默用旧代码判放行**：它看起来在
    拦，实际跑的是另一个版本的判定。工作树现在只作兜底（根目录没有代码时）。
    """
    env = os.environ if env is None else env
    candidates = []
    if env.get("TASK_CENTER_CODE"):
        candidates.append(Path(env["TASK_CENTER_CODE"]))
    candidates.append(Path(repo))
    candidates.append(Path(repo) / ".worktrees" / "ticket-core")
    return candidates


def find_center_root(repo=REPO, env=None):
    """返回第一个真的含 task_center 代码的候选目录；都没有则返回 None。"""
    for base in center_candidates(repo=repo, env=env):
        if (base / "task_center" / "__init__.py").is_file():
            return base
    return None


def load_center():
    """找到 task_center 代码目录，返回 Center 类。"""
    base = find_center_root()
    if base is not None:
        sys.path.insert(0, str(base))
        from task_center import Center

        return Center
    raise SystemExit("找不到 task_center 代码目录；设 TASK_CENTER_CODE 指向它")


def find_db(explicit=None):
    """中心库只能被发现，不能被新建。"""
    if explicit:
        return Path(explicit)
    if os.environ.get("TASK_CENTER_DB"):
        return Path(os.environ["TASK_CENTER_DB"])
    if CONFIG.is_file():
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        if data.get("db"):
            return Path(data["db"])
    return None


def same_root(left, right):
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def git_dir(repo=REPO):
    """返回仓库的真实 git 目录；不是仓库就返回 None。

    工作树（`git worktree add`）里 `.git` 是一个文本文件，内容是 `gitdir: <路径>`，
    指向 `.git/worktrees/<名字>`。合并状态文件（MERGE_HEAD）放在**那个**目录里，
    所以这里必须沿 gitdir 走；直接读 `<repo>/.git/MERGE_HEAD` 在工作树里永远读不到。
    相对路径按 `.git` 文件所在目录解析，这是 git 自己的口径。
    """
    dot = Path(repo) / ".git"
    if dot.is_dir():
        return dot
    if dot.is_file():
        text = dot.read_text(encoding="utf-8", errors="replace").strip()
        if text.lower().startswith("gitdir:"):
            target = Path(text.split(":", 1)[1].strip())
            if not target.is_absolute():
                target = Path(repo) / target
            if target.is_dir():
                return target
        return None
    return None


def merge_in_progress(repo=REPO):
    """仓库当前是否在一次未结束的合并里（存在 MERGE_HEAD）。

    用文件存在判断，而不是 `git rev-parse --verify MERGE_HEAD`：钩子本身就跑在 git
    里，少一次子进程，也少一个「git 不在 PATH 上」的失败面。
    """
    folder = git_dir(repo)
    if folder is None:
        return False
    return (folder / "MERGE_HEAD").is_file()


def decide(ticket_state, *, bound, in_merge, has_merge):
    """闸门判定，纯函数：`running` / `merging` / `reject`。

    两条放行路径，互不重叠：

    - `running`：普通改动。工单在执行中，且当前尝试已绑定本会话。这条规则不变。
    - `merging`：合并提交。`auto_merge` 只在工单验收通过（done）之后才合并，
      所以合并提交发生时工单**已经不是 running**。只看 running 会把每一次合并
      都拦掉，而合并正是「让改动进 master」的那一步。

    合并放行只对 done + 没有合并记录成立：已经记过合并的工单不该出现第二次合并
    提交；ready / dispatching / rework / awaiting_review 即便检出停在半合状态也照旧
    拒绝，别让一个残留的 MERGE_HEAD 变成万用通行证。
    """
    if in_merge and ticket_state == "done" and not has_merge:
        return "merging"
    if bound and ticket_state == "running":
        return "running"
    return "reject"


def registered_project(center_path, repo):
    """按「最深的注册祖先」判断这份工作属于哪个项目，和探针同一口径。"""
    listed = load_center()(center_path).execute("list_projects", {})
    if not listed["ok"]:
        return None, listed["error"]
    repo = Path(repo).resolve()
    best = None
    for item in listed["result"]["items"]:
        root = Path(item["root"]).resolve()
        if same_root(root, repo) or repo.is_relative_to(root):
            if best is None or len(root.parts) > len(Path(best["root"]).parts):
                best = item
    return best, None


def verify(db_path, ticket_id, project=None, repo=REPO, in_merge=None):
    """返回 (是否放行, 载荷)。拒绝时载荷里带一句能照做的补救。

    `in_merge` 留空时按 `repo` 现场判断（有 MERGE_HEAD 就算），测试可以显式指定。
    """
    if db_path is None or not Path(db_path).is_file():
        return False, {
            "error": "DB_NOT_FOUND",
            "fix": "中心库没找到。检查 ~/.agents/center.json 的 db 字段指向的库是否存在；"
                   "不要为了让闸门通过而新建一个库。",
        }
    center = load_center()(db_path)
    # 项目归属一律从仓库路径反查，不接受调用方指定另一个项目。
    # 以前 "project 为空才反查" 可以被一个残留的 TASK_CENTER_PROJECT 直接旁路：
    # 在 A 项目目录里拿 B 项目的工单背书，闸门照样放行。
    found, error = registered_project(db_path, repo)
    if error is not None:
        return False, {"error": error["code"], "fix": str(error["message"])}
    if found is None:
        return False, {
            "error": "PROJECT_NOT_REGISTERED",
            "fix": f"{Path(repo).resolve()} 不属于任何已注册项目。先 register_project，再建工单。",
        }
    if project is not None and project != found["project_id"]:
        return False, {
            "error": "PROJECT_MISMATCH",
            "fix": f"PROJECT_MISMATCH：{Path(repo).resolve()} 属于 {found['project_id']}，"
                   f"不接受用 {project} 的工单背书。去掉 --project / TASK_CENTER_PROJECT 重试。",
        }
    project = found["project_id"]
    ticket = center.execute("get_ticket", {"project_id": project, "ticket_id": ticket_id})
    if not ticket["ok"]:
        code = ticket["error"]["code"]
        if code == "NOT_FOUND":
            fix = (f"这张工单不存在。先在项目 {project} 里创建工单（create_ticket），"
                   "再 claim_ticket，再 bind_session，然后才动文件。")
        elif code == "PROJECT_MISMATCH":
            fix = (f"PROJECT_MISMATCH：这张工单不属于本项目 {project}。"
                   "要么换工单，要么确认你是不是在别人的项目目录里改东西。")
        else:
            fix = ticket["error"]["message"]
        return False, {"error": code, "fix": fix}
    current = ticket["result"]
    state = current["state"]
    attempt = next((item for item in current["attempts"] if item["id"] == current["current_attempt"]), None)
    bound = bool(attempt is not None and attempt["state"] == "running" and attempt["session_id"])
    merging = merge_in_progress(repo) if in_merge is None else bool(in_merge)
    has_merge = bool(current.get("merges"))
    verdict = decide(state, bound=bound, in_merge=merging, has_merge=has_merge)
    if verdict != "reject":
        return True, {
            "mode": verdict,
            "merging": merging,
            "ticket_id": current["id"],
            "project_id": current["project_id"],
            "title": current["title"],
            "deliverable": current["deliverable"],
            "prd_path": current["prd_path"],
            "revision": current["revision"],
            "attempt_id": attempt["id"] if attempt is not None else None,
            "session_id": attempt["session_id"] if attempt is not None else None,
            "state": state,
        }
    if state == "done" and has_merge:
        return False, {
            "error": "ALREADY_MERGED",
            "state": state,
            "fix": "这张工单已经有合并记录，合并不该再做第二次。先读 get_ticket 的 merges 核对实际合并情况。",
        }
    fixes = {
        "ready": "工单还没领取。跑 claim_ticket（带 expected_revision），然后 bind_session 绑定本会话。",
        "rework": "工单在待返工。重新 claim_ticket 会产生新尝试，然后 bind_session。",
        "dispatching": "已经 claim 但还没绑定会话。跑 bind_session，把 session_id 和项目 root 传进去。",
        "awaiting_review": "成果已提交、等着验收。要改东西就先 recover_ticket 或 revise_ticket，不要直接改。",
        "done": "工单已完成。要再改就新建工单，或 revise_ticket 且显式 reopen；"
                "如果这一步其实是合并提交，确认仓库里真的有未结束的合并（MERGE_HEAD），"
                "合并要走 tools/auto_merge.py --ticket <编号>，别手动拼一个。",
        "cancelled": "工单已取消，不能拿它当改动依据。新建一张。",
    }
    return False, {
        "error": f"TICKET_{state.upper()}",
        "state": state,
        "fix": fixes.get(state, "工单不在可执行状态；读 get_ticket 看当前状态再决定。"),
    }


class CenterUnreadable(RuntimeError):
    """库读不了：schema 版本不匹配、文件损坏、升到一半。

    这不是「没有数据」，是「不知道有没有数据」，两者必须分得开。以前 `active()`
    读库失败就 `return []`，`--list` 于是打印零行、退出 0——它告诉所有人「没有活跃
    尝试」，而它正是「开工前先查有没有人在做」赖以判断的那条命令。撒谎的后果是两个
    人同时改同一批文件，所以这里抛异常，让调用方非零退出并说出错误码。
    """

    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def active(db_path):
    """列出所有带活跃尝试的工单：谁在做什么，一眼能看到。

    任何一步读失败都抛 `CenterUnreadable`：只列出读得通的那部分同样是撒谎，
    少掉的恰好可能是别人正在做的那张单。
    """
    center = load_center()(db_path)
    items = []
    projects = center.execute("list_projects", {})
    if not projects["ok"]:
        raise CenterUnreadable(projects["error"]["code"], projects["error"]["message"])
    for project in projects["result"]["items"]:
        listed = center.execute("list_tickets", {"project_id": project["project_id"], "limit": 100})
        if not listed["ok"]:
            raise CenterUnreadable(listed["error"]["code"], listed["error"]["message"])
        for ticket in listed["result"]["items"]:
            detail = center.execute("get_ticket", {"project_id": project["project_id"], "ticket_id": ticket["id"]})
            if not detail["ok"]:
                raise CenterUnreadable(detail["error"]["code"], detail["error"]["message"])
            for attempt in detail["result"]["attempts"]:
                if attempt["id"] == detail["result"]["current_attempt"] and attempt["state"] in ("dispatching", "running"):
                    items.append({
                        "project_id": project["project_id"], "ticket_id": ticket["id"],
                        "attempt_id": attempt["id"], "session_id": attempt["session_id"],
                        "state": attempt["state"], "created_at": attempt["created_at"],
                    })
    return items


def read_staged_paths(repo=REPO):
    """返回 (暂存区路径列表, 错误说明)。拿不到暂存区时列表为 None。"""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "--name-only"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"跑不了 `git diff --cached`（{exc}）"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[:1]
        return None, f"`git diff --cached` 退出 {result.returncode}（{detail[0] if detail else '没有输出'}）"
    return [line.strip() for line in result.stdout.splitlines() if line.strip()], None


def round_changed_lines(repo=REPO):
    """`tasks/round.md` 的暂存增删行（不含 +++/--- 头）。返回 (行列表, 错误说明)。"""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "-U0", "--", "tasks/round.md"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"跑不了 `git diff --cached -- tasks/round.md`（{exc}）"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[:1]
        return None, f"取 `tasks/round.md` 的暂存 diff 失败（{detail[0] if detail else result.returncode}）"
    return [line for line in result.stdout.splitlines() if ROUND_CHANGED_LINE.match(line)], None


def read_round(repo=REPO):
    """读 `tasks/round.md`，返回 (轮次, 状态, 错误说明)。"""
    path = Path(repo) / "tasks" / "round.md"
    if not path.is_file():
        return None, None, f"{path}: 没有本轮目标文件"
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return None, None, f"{path}: 不是 UTF-8 文件（第 {exc.start} 字节无法解码）"
    number = ROUND_NUMBER_LINE.search(text)
    status = ROUND_STATUS_LINE.search(text)
    return (number.group(1) if number else None), (status.group(1) if status else None), None


def project_tickets(center_path, repo=REPO):
    """返回 (项目, 工单详情列表)；读不出来一律抛 CenterUnreadable。"""
    found, error = registered_project(center_path, repo)
    if error is not None:
        raise CenterUnreadable(error["code"], str(error["message"]))
    if found is None:
        raise CenterUnreadable("PROJECT_NOT_REGISTERED", f"{Path(repo).resolve()} 不属于任何已注册项目")
    center = load_center()(center_path)
    tickets = []
    offset = 0
    while True:  # 一页最多 100 张：翻完为止，不把「没翻到」当成「没有」
        listed = center.execute("list_tickets", {"project_id": found["project_id"], "limit": 100, "offset": offset})
        if not listed["ok"]:
            raise CenterUnreadable(listed["error"]["code"], listed["error"]["message"])
        page = listed["result"]["items"]
        for item in page:
            detail = center.execute("get_ticket", {"project_id": found["project_id"], "ticket_id": item["id"]})
            if not detail["ok"]:
                raise CenterUnreadable(detail["error"]["code"], detail["error"]["message"])
            tickets.append(detail["result"])
        if len(page) < 100:
            break
        offset += len(page)
    return found, tickets


def closing_commit(repo=REPO, center_path=None, staged_paths=None):
    """收尾提交：第三条放行路径。

    为什么它必须存在：收尾包只能在「本项目没有任何工单在飞」的时刻提交（`close_gate
    --check` 要求无在途单），而普通改动的放行要求一张绑定在 `running` 尝试上的工单。
    两者不可能同时成立，所以收尾提交必须像合并提交那样被显式承认。

    三条前提同时成立才放行，其余一律拒（`decide()` 一字未动，新逻辑只在这里）：

    1. 暂存路径全部落在 `tasks/closing-*.md` / `tasks/round.md` / `tasks/rounds.md`；
    2. 本项目在途集合为空（含还没开始的 ready / rework）；
    3. `tasks/round.md` 的暂存增删每一行都含 `状态：`，且至少有一份收尾包。

    返回 `{"verdict": "skip" | "allow" | "reject", "round": ..., "detail": ...}`。
    三态是必要的：`skip` 交给普通规则（`decide()`），`reject` 不能落回普通规则——
    否则一张 `running` 的工单就能为夹带的收尾提交背书，例外等于没设。拿不到暂存区、
    拿不到库、读不到轮次行时一律 `reject` 并写明原因，不许因为「没报错」而放行。
    """
    repo = Path(repo)
    if staged_paths is None:
        staged_paths, error = read_staged_paths(repo)
        if error is not None:
            return {"verdict": "reject", "round": None,
                    "detail": f"拿不到暂存区：{error}。收尾提交要按暂存区一条条核，核不了就不放行。"}
    paths = [str(path) for path in staged_paths]
    packets = [path for path in paths if CLOSING_PACKET.match(path)]
    outside = [path for path in paths if path not in CLOSING_EXACT_PATHS and not CLOSING_PACKET.match(path)]

    edits, error = round_changed_lines(repo)
    round_touched = "tasks/round.md" in paths
    if round_touched and error is not None:
        return {"verdict": "reject", "round": None,
                "detail": f"拿不到 tasks/round.md 的暂存 diff：{error}。判不出它只改了状态行，就不放行。"}
    status_only = bool(edits) and all(ROUND_STATUS_ANYWHERE.search(line) for line in edits)

    # 只有「带收尾包」或「round.md 只改了状态行且没夹带别的文件」才可能是收尾提交；
    # 其余交回普通规则。不放宽成「只要碰了 round.md 就按收尾提交判」：那会让一张
    # running 的工单正常改代码时被一条与它不相干的例外拦住。
    if not packets and not (round_touched and status_only and not outside):
        return {"verdict": "skip", "round": None,
                "detail": "暂存区里没有收尾包、也不只是状态行改动；按普通提交规则判。"}
    if outside:
        return {"verdict": "reject", "round": None,
                "detail": ("收尾提交只允许 tasks/closing-*.md / tasks/round.md / tasks/rounds.md，"
                           f"这次还带了：{'、'.join(outside)}。别的文件要走工单，不能搭这一趟。")}
    if not packets:
        return {"verdict": "reject", "round": None,
                "detail": ("收尾提交必须带一份 tasks/closing-<轮次>.md；只改状态位不算收尾，"
                           "七节收尾包按 docs/closing.md 写。")}
    if round_touched and not status_only:
        bad = [line for line in edits if not ROUND_STATUS_ANYWHERE.search(line)]
        sample = "、".join(line.strip() for line in bad[:3])
        return {"verdict": "reject", "round": None,
                "detail": ("tasks/round.md 在收尾提交里只许改状态行，这次还改了："
                           f"{sample}。轮次行、本轮目标、结束条件都要走工单。")}

    round_no, status, error = read_round(repo)
    if error is not None or round_no is None:
        return {"verdict": "reject", "round": None,
                "detail": (f"读不到 tasks/round.md 的 `轮次：R-00N`（{error or '没有这一行'}）；"
                           "收尾提交要报出是哪一轮，读不到就不放行。")}

    if center_path is None or not Path(center_path).is_file():
        return {"verdict": "reject", "round": round_no,
                "detail": ("读不了中心库：不知道本项目还有没有在途工单，就不放行。"
                           "检查 ~/.agents/center.json 的 db 字段，不要为了让闸门通过新建一个库。")}
    try:
        found, tickets = project_tickets(center_path, repo)
    except CenterUnreadable as error:
        return {"verdict": "reject", "round": round_no,
                "detail": f"读不了中心库：{error.code}（{error.message}）；不知道有没有在途工单，就不放行。"}

    blocking = [f"{ticket['id']}（{ticket['state']}）" for ticket in tickets
                if ticket["state"] in CLOSING_BLOCKING_STATES]
    if blocking:
        return {"verdict": "reject", "round": round_no,
                "detail": (f"项目 {found['project_id']} 还有 {len(blocking)} 张在途工单：{'、'.join(blocking)}；"
                           "close_gate 的 --check 也不会过。先等它验收或走 recover_ticket，再收尾。")}

    return {"verdict": "allow", "round": round_no,
            "detail": f"收尾提交：{round_no}"}


def unclosed_round(center_path, repo=REPO):
    """本轮该收却没收回：返回 (轮次, 说明) 或 (None, None)。

    三个条件同时成立才报：本项目在途集合为空、`tasks/round.md` 的状态是「进行中」、
    且本项目存在 done 且有合并记录的工单。成立时 `--list` 非零并给三步——它是每轮
    开工的第一条命令，`状态：进行中` 不该能合法地永远留着。

    读不出轮次文件（比如别的仓库没有 tasks/）、或项目没注册，就不报：那是「判不了」，
    不是「收过了」。库读不了仍抛 `CenterUnreadable`，由 `--list` 按既有出口报错。
    """
    round_no, status, _error = read_round(repo)
    if round_no is None or status is None or status in ROUND_CLOSED_STATUSES:
        return None, None
    try:
        found, tickets = project_tickets(center_path, repo)
    except CenterUnreadable as error:
        if error.code == "PROJECT_NOT_REGISTERED":
            return None, None
        raise
    if any(ticket["state"] in CLOSING_BLOCKING_STATES for ticket in tickets):
        return None, None
    landed = [ticket["id"] for ticket in tickets if ticket["state"] == "done" and ticket.get("merges")]
    if not landed:
        return None, None
    target = Path(repo) / "tasks" / f"closing-{round_no}.md"
    return round_no, (
        f"本轮 {round_no} 未收尾：项目 {found['project_id']} 已经没有在途工单，已有验收并合并的工单"
        f"（{'、'.join(landed[:3])}{' 等' if len(landed) > 3 else ''}），但 tasks/round.md 还写着 状态：{status}。\n"
        "这样补（三步）：\n"
        f"  1. 按 docs/closing.md 写 {target}（七节，第 7 节结论三档之一）\n"
        "  2. 把 tasks/round.md 的状态位改成与结论一致（已收尾 / 等一句答复 / 需重开）\n"
        "  3. git add 这两个文件（外加 tasks/rounds.md 的历史行）后直接 git commit，"
        f"钩子把它认成「收尾提交：{round_no}」例外，不要 TASK_CENTER_TICKET\n"
        "本轮没收尾就先收尾再开新单。"
    )


def main(argv):
    # 钩子与测试都按 UTF-8 读，本机控制台默认编码（cp936）会把中文读成乱码。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="改动前核对工单")
    parser.add_argument("--ticket", default=os.environ.get("TASK_CENTER_TICKET"))
    parser.add_argument("--project", default=os.environ.get("TASK_CENTER_PROJECT"))
    parser.add_argument("--repo", default=str(REPO))
    parser.add_argument("--db", default=None)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv[1:])

    db_path = find_db(args.db)
    if args.list:
        if db_path is None or not Path(db_path).is_file():
            print("中心库没找到：检查 ~/.agents/center.json 的 db 字段", file=sys.stderr)
            return 1
        try:
            items = active(db_path)
        except CenterUnreadable as error:
            # 读不了库就必须非零退出：零行 + 退出 0 等于宣称「没有人在做」。
            print(f"读库失败：{error.code}（{db_path}）", file=sys.stderr)
            print(f"  {error.message}", file=sys.stderr)
            print("  这不是「没有活跃尝试」，是「查不出来」。先修库"
                  "（migrate 或恢复快照），再重新跑 --list；不要把它当成没人干活。",
                  file=sys.stderr)
            return 1
        for item in items:
            print(f"{item['ticket_id']}\t{item['project_id']}\t{item['state']}\t{item['session_id'] or '(未绑定)'}")
        try:
            round_no, unclosed = unclosed_round(db_path, args.repo)
        except CenterUnreadable as error:
            # 清单已经打印出来了，但「本轮收了没有」判不了：同样非零，不说「没事」。
            print(f"读库失败：{error.code}（{db_path}）", file=sys.stderr)
            print(f"  {error.message}", file=sys.stderr)
            return 1
        if unclosed is not None:
            print(unclosed, file=sys.stderr)
            print(f"本轮 {round_no} 没有收尾：先收尾再开新单（判据与落点见 docs/closing.md）。", file=sys.stderr)
            return 1
        return 0
    # 收尾提交判定必须排在「没有工单号就拒」之前：收尾包本来就不该有工单能背书。
    closing = closing_commit(args.repo, db_path)
    if closing["verdict"] == "allow":
        if args.json:
            print(json.dumps({"ok": True, "result": {"mode": "closing", "round": closing["round"],
                                                      "detail": closing["detail"]}}, ensure_ascii=False, indent=2))
        else:
            print(closing["detail"])
            print("  放行理由：暂存区只含收尾包与状态行，本项目没有在途工单；"
                  "这是收尾，不是普通改动，所以不需要 TASK_CENTER_TICKET。")
        return 0
    if closing["verdict"] == "reject":
        if args.json:
            print(json.dumps({"ok": False, "result": {"mode": "closing", "error": "CLOSING_REJECTED",
                                                      "detail": closing["detail"]}}, ensure_ascii=False, indent=2))
        else:
            print("拦下：CLOSING_REJECTED（这次提交想当收尾提交，但前提不成立）", file=sys.stderr)
            print(closing["detail"], file=sys.stderr)
        return 1
    if not args.ticket:
        print("没有工单号。先 claim_ticket + bind_session，然后用 "
              "--ticket <编号> 或环境变量 TASK_CENTER_TICKET 出示它。", file=sys.stderr)
        return 1
    ok, payload = verify(db_path, args.ticket, project=args.project, repo=args.repo)
    if args.json:
        print(json.dumps({"ok": ok, "result": payload}, ensure_ascii=False, indent=2))
    elif ok and payload.get("mode") == "merging":
        # 合并提交走这里：工单已 done，靠 MERGE_HEAD 认出「这次提交是合并」。
        print(f"合并提交：{payload['ticket_id']}（{payload['project_id']}）state=done，尚无合并记录")
        print(f"  交付物：{payload['deliverable']}")
        print(f"  PRD：{payload['prd_path']}  revision {payload['revision']}")
        print("  放行理由：仓库处于未结束的合并中（MERGE_HEAD 存在）；这是合并，不是普通改动。")
    elif ok:
        print(f"放行：{payload['ticket_id']}（{payload['project_id']}）{payload['title']}")
        print(f"  交付物：{payload['deliverable']}")
        print(f"  PRD：{payload['prd_path']}  revision {payload['revision']}  会话 {payload['session_id']}")
    else:
        print(f"拦下：{payload['error']}", file=sys.stderr)
        print(payload["fix"], file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
