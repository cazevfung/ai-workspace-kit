"""工单闸门：回答「这次改动凭什么可以落盘」。

前两轮的真实问题不是没人知道该开工单，而是**没有一步会因此失败**：计划文件过了
闸门，代码提交了，中心库里的工单数仍然是 0。这个脚本把「有没有工单、有没有绑定
到本会话的活跃尝试」变成一个退出码，并被两个仓库的 commit 钩子调用。

用法：
    python tools/work_gate.py --ticket T-1              # 通过则退出 0
    python tools/work_gate.py --ticket T-1 --repo {{WORKSPACE}}
    python tools/work_gate.py --list                     # 现在有哪些活跃尝试 + 在途集合长什么样（读不了库则非零退出）
    python tools/work_gate.py --list --json               # 同一份清单，机器可读
    python tools/work_gate.py --list --stalled-minutes 45 # 有单超过这个分钟数没落库就退出 1
    python tools/work_gate.py --ticket T-1 --json

钩子里的约定：`TASK_CENTER_TICKET` 给出工单号，`TASK_CENTER_PROJECT` 可选。
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import center_config

REPO = Path(__file__).resolve().parent.parent
# 中心库的发现顺序只在 `tools/center_config.py` 一处：这里、`auto_merge`、`close_gate`
# 与两个巡检工具都从它取，不再各写一遍 `~/.agents/center.json`——AWK-105 的现场是
# `--agents-home` 装出来的工作区，闸门却读到 `~/.agents` 那一套库。
# 停住阈值默认 45 分钟（PRD-013 R4）。不传 --stalled-minutes 时这个值只用来算
# 「其中 M 张最后落库超过 45 分钟」那一句；要不要因此非零退出，见 main() 里的说明。
DEFAULT_STALLED_MINUTES = 45

# 收尾提交（例外路径）只放开这三个位置。多一条路径就是多一扇门，将来要扩就显式改这里。
CLOSING_EXACT_PATHS = ("tasks/round.md", "tasks/rounds.md")
CLOSING_PACKET = re.compile(r"^tasks/closing-[^/]+\.md$")
# round.md 在收尾提交里只许改状态行：逐行看增删，不按行数糊过去。
ROUND_CHANGED_LINE = re.compile(r"^[-+](?![-+][-+])")
ROUND_STATUS_ANYWHERE = re.compile(r"状态[：:]")
ROUND_NUMBER_LINE = re.compile(r"^轮次[：:]\s*(R-\d{3})\s*$", re.M)
ROUND_STATUS_LINE = re.compile(r"^状态[：:]\s*(\S+)\s*$", re.M)
# 轮次归属哪个项目：`项目：<project_id>`，只认顶部（`round_header()`）。三个项目共用一条
# 编号线（全库唯一，见 `docs/closing.md` 的「轮次编号与账本」），所以轮次文件必须自己
# 说是哪个项目的轮——`--list` 的「本轮未收尾」按这一行判。
ROUND_PROJECT_LINE = re.compile(r"^项目[：:]\s*(\S+)\s*$", re.M)
# 状态位是三档之一时这一轮已经有结论；只有「进行中」才可能是没收。
ROUND_CLOSED_STATUSES = ("已收尾", "等一句答复", "需重开")
# 在途集合：**只有这一份定义**。`docs/closing.md` 的「在途集合只有一份定义」一节写的
# 就是它，`tools/close_gate.py` 从这里导入、`--list` 也按它报（PRD-014 R1）。
#
# 为什么必须一份：两条判定问的是同一件事「本项目还有没有没干完的活」，各写一份就会
# 各给一个答案。AW-094 就是那样：`close_gate --check` 说可以收（它只有四个状态），
# 收尾提交的钩子连拒三次（它把 `ready` 也算上），最后只能把排好的单取消掉才收得了尾。
#
# `planned` **不在其中**：那是「还没轮到、在库里排着」，不产生尝试、没有人在做，
# 排着队的单不该挡住一轮的收尾。`ready` / `rework` 仍在其中：那是「等人领走」，
# 是没干完的活。
CLOSING_BLOCKING_STATES = ("dispatching", "running", "awaiting_review", "blocked", "ready", "rework")
# 在途里「没有会话在做、只在等人领走」的那两格：`--list` 据它判红并点名验收条件
# AC-R1-2（`ready` 在场时同一条命令必须非零）。其余四格都有尝试、有会话在做，
# 不该因为别人在跑就让 --list 变成一条恒红的命令。
WAITING_STATES = ("ready", "rework")
# 池子的年龄线（AW-109）：一张 planned 排够这么多天还没动，就该被举到人眼前。
# 为什么是 7：一轮（R-0NN）通常 1–2 天，7 天 = 跳 3–5 轮还没人碰，按「没人打算做」处理。
# 不自动清理——`cancel_ticket` 是终止、不可自动恢复（pdsa-workflow），机器只管把念头举起来，
# 不替人决定念头作废。这一处是全仓唯一的定义，`describe_pool()` 与 docs/closing.md 都按它说。
PLANNED_MAX_DAYS = 7


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


def discover_db(explicit=None, repo=REPO, **kwargs):
    """按 `center_config` 的顺序发现中心库，返回 `Resolution`；找不到抛 `DiscoveryError`。"""
    return center_config.resolve(repo=repo, explicit=explicit, **kwargs)


def find_db(explicit=None, repo=REPO, **kwargs):
    """中心库只能被发现，不能被新建。

    保留为 `discover_db(...).db` 的薄封装：老调方拿到 `None` 就知道「没找到」，
    要错误码与那句「凭什么」的用 `discover_db`。两处都不自己解顺序。
    """
    try:
        return discover_db(explicit, repo=repo, **kwargs).db
    except center_config.DiscoveryError:
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
            "fix": f"中心库没找到（{db_path}）。" + center_config.ORDER_HINT,
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
        "awaiting_review": "成果已提交、等着验收。要改东西就先 recover_ticket，"
                           "或 revise_ticket 带 restart=true（不带 restart 时判据会改、这一代仍不换，槽位照旧占着）。",
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

    `step` 是出错的那一步（操作名，例如 `list_projects`）：报错时把它打出来，
    「照着操作名与报文定位」才不是一句空话（AW-165）。
    """

    def __init__(self, code, message, step=None):
        self.code = code
        self.message = message
        self.step = step
        super().__init__(f"{code}: {message}")


# 「整库读不出」时的分诊（AW-165）。以前两个分支都只写一句「先修库（migrate 或
# 恢复快照）」：那句话在起因现场（一行历史 JSON 坏掉）方向是反的，而 AW-163 把「单张
# 单读不出来」分开之后，能走到这两处的只剩下面几类，它们的出路并不相同——
#   schema 不符 → migrate；库文件坏 → 恢复快照或换 --db；
#   其余（锁 / 权限 / 打不开 / 未知）→ 照着操作名、异常类型与报文定位，不猜方向。
_CORRUPT_MARKERS = ("not a database", "malformed", "corrupt", "encrypted", "disk image")
_BUSY_MARKERS = ("locked", "busy")


def exception_kind(exc):
    """把一个存储层异常归到分诊的那几类现场之一：corrupt / busy / closed / unknown。"""
    if isinstance(exc, OSError):
        return "closed"
    text = str(exc).lower()
    if any(marker in text for marker in _CORRUPT_MARKERS):
        return "corrupt"
    if any(marker in text for marker in _BUSY_MARKERS):
        return "busy"
    if "unable to open" in text:
        return "closed"
    return "unknown"


def probe_center(db_path):
    """整库读不出时自己再读一遍这个库：只读，不建表、不写行、不跑 migrate。

    为什么还要读一遍：core 把存储层异常压成一句人话（`STORAGE_ERROR: file is not a
    database`），里头没有异常类型；而「照哪一类现场去修」看的正是它。返回
    `(kind, detail)`：kind 见 `exception_kind()`，读得通时是 `"readable"`。
    """
    target = Path(db_path)
    try:
        conn = sqlite3.connect(f"{target.absolute().as_uri()}?mode=rw", uri=True, timeout=2)
    except (sqlite3.Error, OSError) as exc:
        return exception_kind(exc), f"{type(exc).__name__}: {exc}"
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except (sqlite3.Error, OSError) as exc:
        return exception_kind(exc), f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()
    return "readable", ""


def unreadable_advice(code, message, probe=None):
    """整库读不出时按现场给一句能当场照做的出路，不再一律「先修库」（AW-165）。

    `probe` 是 `probe_center()` 的结果；没给时就只按错误码与报文判。三类出路与现场
    一一对应：把三类混成一句「重启试试」，等于三个方向都不说。
    """
    code = (code or "").upper()
    kind, detail = probe or ("", "")
    text = (message or "").lower()
    store = detail or message or "见上一行报文"
    if code == "SCHEMA_MISMATCH":
        return ("分诊：代码与库不是同一版（schema 对不上）→ 先复制一份库，再跑 "
                "python -m task_center --db <这个库> migrate，升完再跑 --list。"
                "升上去之前不要在这个库上开工。")
    if kind == "corrupt" or (not kind and any(marker in text for marker in _CORRUPT_MARKERS)):
        return (f"分诊：库文件本身坏了（存储层：{store}）→ 用最近一次文件层快照恢复"
                "（合并工具在迁移前会留一份），或用 --db 指到一份完好的库。"
                "文件坏了 migrate 只会再失败一次，别对着它跑。")
    path_note = "（「打不开」这一种先看那条路径在不在、当前账号读不读得了）" \
        if kind == "closed" else ""
    return (f"分诊：这不是 schema 不符，也不是文件坏（存储层：{store}），归「锁 / 权限 / "
            f"打不开 / 未知」这一档{path_note} → 照着上面的操作名、异常类型与报文定位："
            "谁连着这个库、那条路径当前账号读不读得了；不猜方向，也别拿 migrate 试。")


def print_unreadable(error, db_path, verdict, stream=None):
    """「整库读不出」的一整组输出：错误码、出错的一步、报文、这一句什么意思、分诊。

    两处分支共用：`--list` 那处判的是「谁在做」，轮次那处判的是「这一轮收没收」，
    现场却是同一个、出路也是同一条——各写一份就必然一边改了、一边没改（AW-165）。
    """
    stream = stream or sys.stderr
    probe = probe_center(db_path)
    print(f"读库失败：{error.code}（{db_path}）", file=stream)
    where = f"出错的一步：{error.step}；" if getattr(error, "step", None) else ""
    print(f"  {where}报文：{error.message}", file=stream)
    if probe[1]:
        print(f"  存储层：{probe[1]}", file=stream)
    print(f"  {verdict}", file=stream)
    print(f"  {unreadable_advice(error.code, error.message, probe)}", file=stream)


def active(db_path, unreadable=None):
    """列出所有带活跃尝试的工单：谁在做什么、等了多久，一眼能看到。

    每条带 `started_at`（尝试创建时间）与 `last_event_at`（本单最后一条事件时间）。
    两个时间是这个清单的用处所在：「谁该动手」看前四列，「等了多久」看后两列。

    **一张单读不出来，不再把整份清单判成读不出来（AW-163）。** 以前这里遇到任何一张单
    的 `get_ticket` 失败就抛 `CenterUnreadable`，于是一行 `INVALID_INPUT` 让整张在途
    清单消失——`HSE-007` 的一条旧提交就干过这件事，代价是所有人都看不见别人在做什么。
    现在分两层：库本身读不了（schema、文件损坏、连不上）照旧抛，那确实是「不知道有没有
    数据」；单张单读不出来则点名记进 `unreadable`（调用方传进来的列表，每项带
    `project_id` / `ticket_id` / `code` / `message`），其余照常列出。少掉的单照样看得见，
    只是不再用它换掉整张表。
    """
    center = load_center()(db_path)
    items = []
    projects = center.execute("list_projects", {})
    if not projects["ok"]:
        raise CenterUnreadable(projects["error"]["code"], projects["error"]["message"],
                               step="list_projects")
    for project in projects["result"]["items"]:
        try:
            tickets = pages(center, project["project_id"])
        except CenterUnreadable as error:
            record_unreadable(unreadable, project["project_id"], None,
                              {"code": error.code, "message": error.message})
            continue
        for ticket in tickets:
            detail = center.execute("get_ticket", {"project_id": project["project_id"], "ticket_id": ticket["id"]})
            if not detail["ok"]:
                record_unreadable(unreadable, project["project_id"], ticket["id"], detail["error"])
                continue
            for attempt in detail["result"]["attempts"]:
                if attempt["id"] == detail["result"]["current_attempt"] and attempt["state"] in ("dispatching", "running"):
                    items.append({
                        "project_id": project["project_id"], "ticket_id": ticket["id"],
                        "attempt_id": attempt["id"], "session_id": attempt["session_id"],
                        "state": attempt["state"], "created_at": attempt["created_at"],
                        "started_at": attempt["created_at"],
                    })
    stamps = last_event_times(db_path, [item["ticket_id"] for item in items])
    for item in items:
        # 没有事件的单用尝试时间兑底：列里永远是能算间隔的时间戳，不是空字符串。
        item["last_event_at"] = stamps.get(item["ticket_id"]) or item["started_at"]
    return items


def record_unreadable(unreadable, project_id, ticket_id, error):
    """记一条「这张单读不出来」：`unreadable` 为 None 时抛 `CenterUnreadable`。

    保留 `None` 这条兜底路径，是因为别的调用方可能只想问「能不能全读通」，不要逐个点名；
    `--list` 传列表进来，于是它拿到的是「其余照常 + 这几张点名」而不是一片空白。
    """
    if unreadable is None:
        raise CenterUnreadable(error["code"], error["message"])
    entry = {"project_id": project_id, "ticket_id": ticket_id,
             "code": error["code"], "message": error["message"]}
    # 同一张单会被两处各读一次（在途表与项目现场），同一件事只点名一行。
    if entry not in unreadable:
        unreadable.append(entry)


def last_event_times(db_path, ticket_ids):
    """一次读出这些工单的最后一条事件时间：`events` 的最大 `created_at`。

    直接读库而不是走 `events` 操作：那个操作按 id 分页返回，要拿「最大时间」得把
    每张单的事件翻完，而这里只需要一个聚合值。只发 SELECT，不建表、不写行。
    """
    ids = [ticket_id for ticket_id in ticket_ids if ticket_id]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        raise CenterUnreadable("STORAGE_ERROR", f"{type(exc).__name__}: {exc}", step="events") from exc
    try:
        rows = conn.execute(
            f"SELECT ticket_id, MAX(created_at) FROM events WHERE ticket_id IN ({placeholders})"
            " GROUP BY ticket_id", ids)
        return {row[0]: row[1] for row in rows if row[1]}
    except sqlite3.Error as exc:
        raise CenterUnreadable("STORAGE_ERROR", f"{type(exc).__name__}: {exc}", step="events") from exc
    finally:
        conn.close()


def stalled(items, threshold_minutes, now=None):
    """返回最后落库超过阈值的条目。读不出时间的条目不算停住：不猜。"""
    now = now or datetime.now(timezone.utc)
    overdue = []
    for item in items:
        try:
            stamp = datetime.fromisoformat(item["last_event_at"])
        except (TypeError, ValueError):
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if (now - stamp).total_seconds() > threshold_minutes * 60:
            overdue.append(item)
    return overdue


def read_staged_changes(repo=REPO):
    """返回 (暂存区每条路径与它的变更类型, 错误说明)。拿不到暂存区时列表为 None。

    每条是 `(状态, 路径, 原路径或 None)`：`-z` 下一条是 `状态\0路径\0`，
    `R*` / `C*` 是 `状态\0原路径\0新路径\0`，路径一律取**这次提交要写到哪儿**的那个。

    为什么不用 `--name-only`：收尾提交的判据要分得开「新增或修改一份收尾包」与
    「删掉一份收尾包」。前者是收尾这个动作，后者只是一次搬迁或清理——2026-11-24
    合 AW-153 时删 `tasks/closing-R-018.md` 就被当成收尾提交，夹带别的文件当场
    `CLOSING_REJECTED`，迁走三份文档只好拆成三笔提交。
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "--name-status", "-z"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"跑不了 `git diff --cached --name-status`（{exc}）"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[:1]
        return None, (f"`git diff --cached --name-status` 退出 {result.returncode}"
                      f"（{detail[0] if detail else '没有输出'}）")
    tokens = result.stdout.split("\0")
    changes, index = [], 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        if not status:
            continue
        if status[0] in ("R", "C"):
            if index + 1 >= len(tokens):
                return None, "暂存区的改名记录不完整（`--name-status -z` 的输出被截断）"
            source, path = tokens[index], tokens[index + 1]
            index += 2
            changes.append((status, path, source))
        else:
            if index >= len(tokens):
                return None, "暂存区记录不完整（`--name-status -z` 的输出被截断）"
            changes.append((status, tokens[index], None))
            index += 1
    return changes, None


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
    """读 `tasks/round.md`，返回 (轮次, 状态, 归属项目, 错误说明)。

    归属项目取顶部（第一个 `## ` 之前）的 `项目：<project_id>`：没有这一行时返回
    `None`，调用方按「当前这个仓库的项目」判——老文件不因为少一行就判不了。
    只认顶部的那一行，是为了不让正文里恰好写着的「项目：X」把归属改掉。
    """
    path = Path(repo) / "tasks" / "round.md"
    if not path.is_file():
        return None, None, None, f"{path}: 没有本轮目标文件"
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return None, None, None, f"{path}: 不是 UTF-8 文件（第 {exc.start} 字节无法解码）"
    number = ROUND_NUMBER_LINE.search(text)
    status = ROUND_STATUS_LINE.search(text)
    owner = ROUND_PROJECT_LINE.search(round_header(text))
    return ((number.group(1) if number else None), (status.group(1) if status else None),
            (owner.group(1) if owner else None), None)


def round_header(text):
    """轮次文件的顶部：第一个二级标题之前的那几行。三行元信息只在这里找。"""
    lines = []
    for line in text.splitlines():
        if line.startswith("## "):
            break
        lines.append(line)
    return "\n".join(lines)


def pages(center, project_id):
    """一个项目的全部工单列表项，翻页翻完。

    以前 `active()` 只取第一页 100 张（AW-163）：ai-workspace 已经超过 100 张，最新建的
    那几张永远不进表——本单自己的 AW-163 当时正在跑，却不在在途清单里。一个会漏掉最新
    工单的「谁在做」清单，比一张单读不出来更安静，也更贵。
    """
    tickets = []
    offset = 0
    while True:
        listed = center.execute("list_tickets", {"project_id": project_id, "limit": 100, "offset": offset})
        if not listed["ok"]:
            raise CenterUnreadable(listed["error"]["code"], listed["error"]["message"],
                                   step="list_tickets")
        page = listed["result"]["items"]
        tickets.extend(page)
        if len(page) < 100:
            return tickets
        offset += len(page)


def tickets_of(center_path, project_id, unreadable=None):
    """按项目号取每一张单的详情：`pages()` 翻完页，再逐张 `get_ticket`。

    与 `project_tickets` 共用这一份翻页：`--list` 现在会读**别的项目**的现场
    （轮次属于谁就按谁判），两边各写一份就会一边翻完、一边只看第一页。
    传了 `unreadable` 时，单张单读不出来就点名后跳过（AW-163）；不传时一律抛
    `CenterUnreadable`——`--ticket` 那道门要的是「能不能全读通」，不是点名。
    """
    center = load_center()(center_path)
    tickets = []
    for item in pages(center, project_id):
        detail = center.execute("get_ticket", {"project_id": project_id, "ticket_id": item["id"]})
        if not detail["ok"]:
            record_unreadable(unreadable, project_id, item["id"], detail["error"])
            continue
        tickets.append(detail["result"])
    return tickets


def project_by_id(center_path, project_id):
    """按项目号取登记记录；没登记返回 None——那是「判不了」，不是「什么都没有」。"""
    listed = load_center()(center_path).execute("list_projects", {})
    if not listed["ok"]:
        raise CenterUnreadable(listed["error"]["code"], listed["error"]["message"],
                               step="list_projects")
    return next((item for item in listed["result"]["items"] if item["project_id"] == project_id), None)


def project_tickets(center_path, repo=REPO, unreadable=None):
    """返回 (项目, 工单详情列表)；没传 `unreadable` 时读不出来一律抛 CenterUnreadable。"""
    found, error = registered_project(center_path, repo)
    if error is not None:
        raise CenterUnreadable(error["code"], str(error["message"]), step="register_project")
    if found is None:
        raise CenterUnreadable("PROJECT_NOT_REGISTERED", f"{Path(repo).resolve()} 不属于任何已注册项目",
                               step="register_project")
    return found, tickets_of(center_path, found["project_id"], unreadable)


def ticket_backing(tickets):
    """找出能给「收尾包改动」背书的那张工单：当前尝试 `running` 且已绑定会话。

    判据与 `verify()` 里那个 `bound` 一字相同（当前尝试 `state == "running"` 且
    `session_id` 非空）：钩子拿不到调用方的会话号，能核的只有「有一张单正在被某个
    会话执行」。`ready` / `rework` 是等人领走、`awaiting_review` 是成果在验收、
    `done` 是已经结束——都不算有人在做，不能给改动背书。
    """
    for ticket in tickets:
        if ticket["state"] != "running":
            continue
        attempt = next((item for item in ticket["attempts"] if item["id"] == ticket["current_attempt"]), None)
        if attempt is not None and attempt["state"] == "running" and attempt["session_id"]:
            return ticket, attempt
    return None


def staged_round_status(edits):
    """从 `tasks/round.md` 的暂存增删行里取出状态位的新值。

    判据与放行无关，只为了让 `packet` 分支的提示说真话：这一支放过时状态位可能
    真的动过（三档之一 → `进行中`，即重开一轮），那句「状态位没动」在那种现场是
    假的。读暂存行而不是工作区文件，是因为要报的就是「这次提交改成了什么」。
    """
    for line in reversed(list(edits or [])):
        if not line.startswith("+"):
            continue
        match = ROUND_STATUS_LINE.search(line[1:])
        if match is not None:
            return match.group(1)
    return None


def closing_commit(repo=REPO, center_path=None):
    """收尾类提交：先问这次提交在不在「收尾」，再问有没有工单。

    两类，判据差一件事：`tasks/round.md` 的状态位有没有改成三档之一
    （`已收尾` / `等一句答复` / `需重开`）。

    - **收尾提交**（状态位改成三档之一）：这就是收尾这个动作本身，必须趁「本项目
      没有在飞的单」时提交（`close_gate --check` 要求无在途单），所以它像合并提交
      那样是一条被显式承认的例外，不需要工单。
    - **收尾包改动**（只动 `tasks/closing-*.md`，状态位没有改成三档之一）：它不是
      收尾，是一条普通改动——给已收尾的轮次补记、改一份收尾包的说法、把状态位从
      三档改回 `进行中`（重开一轮）都属于这一类。普通改动必须有工单背书，所以它按
      `running` 放行而不是按例外放行（AW-124 现场：这类提交以前被一律当成收尾提交，
      于是 `CLOSING_REJECTED`，只能 `--no-verify` 落盘）。

    两类共同的窄条件（一字未放过）：

    1. 暂存路径全部落在 `tasks/closing-*.md` / `tasks/round.md` / `tasks/rounds.md`；
    2. `tasks/round.md` 的暂存增删每一行都含 `状态：`，且至少有一份收尾包；
    3. 收尾提交另要求在途集合为空（含还没开始的 ready / rework，不含池子里的 planned）。

    第 2 条的「至少一份收尾包」只数**新增或修改**的：纯删除（`D`）不算带收尾包，
    否则「搬走一份旧收尾包」会被当成「收尾」，夹带别的文件就当场拒绝。

    两支共用第 1、2 条：收尾包改动允许带 `tasks/round.md`，但只允许改状态行，
    每一增删行都含 `状态：`——与收尾提交同一道白名单，不是另一份。

    返回 `{"verdict": "skip" | "allow" | "reject", "round": ..., "detail": ...}`；
    `allow` 时另带 `mode`（`closing` / `packet`）。三态是必要的：`skip` 交给普通规则（`decide()`），
    `reject` 不能落回普通规则——否则一张 `running` 的工单就能为夹带的收尾类提交背书，
    例外等于没设。拿不到暂存区、拿不到库、读不到轮次行时一律 `reject` 并写明原因，
    不许因为「没报错」而放行。
    """
    repo = Path(repo)
    changes, error = read_staged_changes(repo)
    if error is not None:
        return {"verdict": "reject", "round": None,
                "detail": f"拿不到暂存区：{error}。收尾提交要按暂存区一条条核，核不了就不放行。"}
    paths = [path for _status, path, _source in changes]
    # 收尾包只算「这次要写进去的」：删除一份收尾包不是收尾这个动作，它是搬迁或
    # 清理，走普通规则由工单背书（AW-153 现场，见 read_staged_changes 的说明）。
    packets = [path for status, path, _source in changes
               if CLOSING_PACKET.match(path) and not status.startswith("D")]
    deleted_packets = [path for status, path, _source in changes
                       if CLOSING_PACKET.match(path) and status.startswith("D")]
    outside = [path for path in paths if path not in CLOSING_EXACT_PATHS and not CLOSING_PACKET.match(path)]

    edits, error = round_changed_lines(repo)
    round_touched = "tasks/round.md" in paths
    if round_touched and error is not None:
        return {"verdict": "reject", "round": None,
                "detail": f"拿不到 tasks/round.md 的暂存 diff：{error}。判不出它只改了状态行，就不放行。"}
    status_only = bool(edits) and all(ROUND_STATUS_ANYWHERE.search(line) for line in edits)

    # 只有「带收尾包」或「round.md 只改了状态行」才可能是收尾类提交；其余交回普通规则。
    # `outside` 不进这个条件：只要状态位真被改过，那就是一次收尾（或重开）尝试，
    # 夹带别的文件得当场拒，不能靠「删了一份收尾包」把夹带洗成一次普通改动。
    if not packets and not (round_touched and status_only):
        gone = f"（这次删了 {'、'.join(deleted_packets)}，那不是收尾）" if deleted_packets else ""
        return {"verdict": "skip", "round": None,
                "detail": f"暂存区里没有新增或修改的收尾包、也不只是状态行改动{gone}；按普通提交规则判。"}
    if outside:
        return {"verdict": "reject", "round": None,
                "detail": ("收尾提交只允许 tasks/closing-*.md / tasks/round.md / tasks/rounds.md，"
                           f"这次还带了：{'、'.join(outside)}。别的文件要走工单，不能搭这一趟。")}
    if not packets:
        gone = f"；这次只有删除的收尾包（{'、'.join(deleted_packets)}），没有新增或修改的一份" if deleted_packets else ""
        return {"verdict": "reject", "round": None,
                "detail": ("收尾提交必须带一份 tasks/closing-<轮次>.md；只改状态位不算收尾，"
                           f"七节收尾包按 docs/closing.md 写{gone}。")}
    if round_touched and not status_only:
        bad = [line for line in edits if not ROUND_STATUS_ANYWHERE.search(line)]
        sample = "、".join(line.strip() for line in bad[:3])
        return {"verdict": "reject", "round": None,
                "detail": ("tasks/round.md 在收尾提交里只许改状态行，这次还改了："
                           f"{sample}。轮次行、本轮目标、结束条件都要走工单。")}

    round_no, status, _owner, error = read_round(repo)
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

    # 分岔就在这里：状态位改成三档之一才是「这次提交在收尾」。
    if not (round_touched and status in ROUND_CLOSED_STATUSES):
        # 只改收尾包 = 一条普通改动。放行的依据是工单，不是收尾这条例外：
        # 拿「本项目在途集合为空」去判它只会两面出错——有单在做时拒掉正当改动
        # （AW-124），没单时又给没有依据的改发放行。
        backing = ticket_backing(tickets)
        if backing is None:
            return {"verdict": "reject", "round": round_no,
                    "detail": ("只改了收尾包、tasks/round.md 的状态位没有变成三档之一"
                               "（已收尾 / 等一句答复 / 需重开），所以这不是收尾提交；"
                               f"而项目 {found['project_id']} 里没有一张绑定在 running 尝试上的"
                               "工单能给它背书。改收尾包本身是一条普通改动：先开一张工单、"
                               "claim_ticket、bind_session，再带 TASK_CENTER_TICKET 提交。")}
        ticket, attempt = backing
        # 这一支放过时状态位不一定没动：三档之一 → `进行中`（重开一轮）也走这里。
        # 所以按暂存行说清这次到底改成了什么，不再一律写「状态位没动」。
        if round_touched:
            new_status = staged_round_status(edits)
            status_note = (f"tasks/round.md 的状态位这次改成了「{new_status}」，不在三档之内"
                           "（已收尾 / 等一句答复 / 需重开）" if new_status else
                           "tasks/round.md 这次只改了状态行，但没读出状态位的新值")
        else:
            status_note = "tasks/round.md 这次没有动，状态位没变"
        return {"verdict": "allow", "round": round_no, "mode": "packet",
                "ticket_id": ticket["id"], "attempt_id": attempt["id"],
                "detail": (f"收尾包改动：{round_no} 的收尾包由工单 {ticket['id']} 背书"
                           f"（当前尝试 {attempt['id']} 在 running、已绑定会话 {attempt['session_id']}）。"
                           f"这不是收尾提交：{status_note}；"
                           "所以改收尾包（含把状态位从三档改回进行中）是一条普通改动，按工单走。")}

    blocking = [f"{ticket['id']}（{ticket['state']}）" for ticket in tickets
                if ticket["state"] in CLOSING_BLOCKING_STATES]
    if blocking:
        return {"verdict": "reject", "round": round_no,
                "detail": (f"项目 {found['project_id']} 还有 {len(blocking)} 张在途工单：{'、'.join(blocking)}；"
                           "close_gate 的 --check 也不会过。先等它验收或走 recover_ticket，再收尾。")}

    return {"verdict": "allow", "round": round_no, "mode": "closing",
            "detail": f"收尾提交：{round_no}"}


def project_snapshot(center_path, repo=REPO, unreadable=None):
    """一次读全项目现场，`--list` 的三件事都从这一份快照读。

    三件事是：谁在做（`active()` 的表）、本轮收了没有（`unclosed_round()`）、收尾会不会
    被在途挡（`describe_in_flight()`）。以前后两件各自扫一遍库，现在一次扫完：`--list`
    本来就慢在这里，而三处各自解释一遍状态名，正是「同一件事三个答案」的温床。

    返回 `None` 表示项目没注册——那是「判不了」，不是「什么都没有」，调用方要分开说。
    库读不了仍抛 `CenterUnreadable`；传了 `unreadable` 时单张单读不出来只点名（AW-163）。
    """
    try:
        found, tickets = project_tickets(center_path, repo, unreadable)
    except CenterUnreadable as error:
        if error.code == "PROJECT_NOT_REGISTERED":
            return None
        raise
    return snapshot_from(found["project_id"], tickets)


def snapshot_from(project_id, tickets):
    """把一批工单详情折成 `--list` 要的那份现场。

    单独一个函数的原因是：轮次属于别的项目时，`unclosed_round()` 要拿**那个项目**
    的现场判（轮次属于谁就按谁判），而「在途几张、池子几张、哪些已收尾合并」这套
    折叠不该为此再写一份。
    """
    counts = {state: 0 for state in CLOSING_BLOCKING_STATES}
    waiting = []
    planned = 0
    pool = []
    landed = []
    for ticket in tickets:
        state = ticket["state"]
        if state == "planned":
            planned += 1
            # 池子要逐张看得见（AW-109）：只数一个数，读的人分不出「念头」与「写好的单」，
            # 也就看不出哪一张已经排了很久。这里只收三个字段，不把整张单抱进快照。
            pool.append({"ticket_id": ticket["id"], "created_at": ticket["created_at"],
                         "criteria_n": len(ticket.get("criteria") or [])})
        if state in counts:
            counts[state] += 1
            if state in WAITING_STATES:
                waiting.append(f"{ticket['id']}（{state}）")
        if state == "done" and ticket.get("merges"):
            landed.append(ticket["id"])
    return {
        "project_id": project_id,
        "states": counts,
        "total": sum(counts.values()),
        "waiting": waiting,
        "planned": planned,
        "planned_tickets": pool,
        "landed": landed,
    }


def describe_in_flight(snapshot):
    """在途集合的人话版：六个状态各几张、池子里还排着几张 `planned`。

    六个状态逐个写出来，是因为这一行就是那份定义本身：要核对「代码与
    `docs/closing.md` 逐项对得上」，看这一行比读常量快。
    """
    detail = " / ".join(f"{state} {count}" for state, count in snapshot["states"].items())
    return (f"在途 {snapshot['total']} 张（{detail}）；"
            f"池子里另 {snapshot['planned']} 张 planned 排着，不算在途")


def describe_pool(snapshot, now=None, max_days=None):
    """池子明细：逐张点名、年龄、条件条数，并把超过年龄线的那几张挑出来。

    `describe_in_flight()` 那一行只数张数（那句就是「planned 不算在途」的定义），读的人
    看不出「这一张叫什么、排了多久、条件写完没有」。而「写不出条件的念头」正是那个位置
    的用途（AW-109），所以这里把每一张的本事写在它旁边：

    - 条件 0 条 → 标「条件空着」：它领不走（`claim_ticket` 报 `CRITERIA_NOT_JUDGEABLE`），
      也不会被 `next_action.py` 派出去，出路只有 `revise_ticket` 补条件或 `cancel_ticket`；
    - 年龄超过 `PLANNED_MAX_DAYS` → 进 `stale`，调用方据此判红。入口越轻、池子越长，
      长到没人看就是另一种蒸发，所以年龄线必须有，且必须能让一条命令变红。

    返回 `(lines, stale)`。`lines` 为空表示池子是空的；`stale` 里每项带 `ticket_id` 与
    `days`。时间读不出来时不猜：那一张按「年龄未知」写，不进 `stale`。
    """
    now = now or datetime.now(timezone.utc)
    max_days = PLANNED_MAX_DAYS if max_days is None else max_days
    lines, stale, ages = [], [], []
    for ticket in snapshot.get("planned_tickets") or []:
        days = None
        try:
            stamp = datetime.fromisoformat(ticket["created_at"])
        except (TypeError, ValueError):
            stamp = None
        if stamp is not None:
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            days = (now - stamp).total_seconds() / 86400
            ages.append(days)
        age = "年龄未知" if days is None else f"排了 {days:.1f} 天"
        if ticket["criteria_n"]:
            state = f"{ticket['criteria_n']} 条条件"
        else:
            state = "条件空着 ← 只能 revise_ticket 补条件或 cancel_ticket"
        lines.append(f"池子：{ticket['ticket_id']} planned，{age}，{state}")
        if days is not None and days > max_days:
            stale.append({"ticket_id": ticket["ticket_id"], "days": days})
    if ages:
        lines.append(f"池子最老的一张排了 {max(ages):.1f} 天（年龄线 {max_days} 天）")
    return lines, stale


def unclosed_round(snapshot, repo=REPO, center_path=None):
    """本轮该收却没收：返回 (轮次, 进失败集合的说明, 旁注)。

    三个条件同时成立才报：**这一轮所属的那个项目**在途集合为空、`tasks/round.md`
    的状态是「进行中」、且那个项目存在 done 且有合并记录的工单。成立时 `--list`
    非零并给三步——它是每轮开工的第一条命令，
    `状态：进行中` 不该能合法地永远留着。

    「这一轮属于哪个项目」按 `tasks/round.md` 顶部的 `项目：<project_id>` 认，
    没写就按当前这个检出的项目判（老文件）。归别的项目时用**那个项目**的现场判，
    不拿本项目的账去套：2026-11-24 之前就是那么误判的——工作区 `round.md` 写着业务
    项目的轮，`--list` 拿工作区的在途集合去比，一边把别人的轮报成本项目的欠账，
    一边让那个项目真的没收的轮永远不会被举起来。

    旁注是「不归本项目、或判不了」的那句话，不进制止集合：读不出轮次文件、项目未登记
    这些「判不了」不算「收过了」，也不算「未收尾」。
    """
    round_no, status, declared, _error = read_round(repo)
    if round_no is None or status is None or status in ROUND_CLOSED_STATUSES:
        return None, None, None
    here = snapshot["project_id"] if snapshot is not None else None
    owner = declared or here
    if owner is None:
        return None, None, None
    round_file = Path(repo) / "tasks" / "round.md"
    target_root = Path(repo)
    target_snapshot = snapshot if owner == here else None
    # 状态与落点必须来自**同一个文件**：归别的项目时，状态也得读那个项目自己的
    # round.md。否则会出现「拿本检出的状态、却叫你去改别人的文件」：照它做，
    # 报上来的那份一字未动，下一轮 `--list` 照样红。
    status_file = "tasks/round.md"
    if owner != here:
        found = project_by_id(center_path, owner) if center_path else None
        if found is None:
            return None, None, (
                f"本轮 {round_no} 属于项目 {owner}（{round_file} 的 `项目：` 行），"
                f"但 {owner} 没登记在库里，判不了它收没收；本项目不为它报。")
        target_root = Path(found["root"])
        remote_round = target_root / "tasks" / "round.md"
        remote_no, remote_status, _remote_owner, _error = read_round(target_root)
        if remote_no is None or remote_status is None:
            return None, None, (
                f"本轮 {round_no} 属于项目 {owner}（{round_file} 的 `项目：` 行），但 {remote_round} "
                "读不到轮次与状态，判不了它收没收；本项目不为它报。")
        if remote_no != round_no:
            return None, None, (
                f"{round_file} 说这一轮是 {round_no}、归项目 {owner}，但 {remote_round} 写的是 "
                f"{remote_no}；两边对不上，既不判也不替谁改，先把两边的轮次改一致。")
        status_file = str(remote_round)
        if remote_status in ROUND_CLOSED_STATUSES:
            return None, None, (
                f"本轮 {round_no} 属于项目 {owner}；那边写着 状态：{remote_status}，本项目不为它报。")
        status = remote_status
        target_snapshot = snapshot_from(owner, tickets_of(center_path, owner))
    if target_snapshot is None:
        return None, None, None
    landed = target_snapshot["landed"]
    if target_snapshot["total"]:
        if owner == here:
            return None, None, None
        return None, None, (
            f"本轮 {round_no} 属于项目 {owner}（{round_file} 的 `项目：` 行）；"
            f"那边还有 {target_snapshot['total']} 张在途单，这一轮本来就收不了；本项目不为它报。")
    if not landed:
        if owner == here:
            return None, None, None
        return None, None, (
            f"本轮 {round_no} 属于项目 {owner}（{round_file} 的 `项目：` 行）；"
            "那边还没有验收并合并的工单，判不出它该收尾；本项目不为它报。")
    target = target_root / "tasks" / f"closing-{round_no}.md"
    where = "" if owner == here else f"属于项目 {owner}，"
    message = (
        f"本轮 {round_no} {where}未收尾：项目 {owner} 已经没有在途工单，已有验收并合并的工单"
        f"（{'、'.join(landed[:3])}{' 等' if len(landed) > 3 else ''}），但 {status_file} 还写着 状态：{status}。\n"
        "这样补（三步）：\n"
        f"  1. 按 docs/closing.md 写 {target}（七节，第 7 节结论三档之一）\n"
        f"  2. 把 {status_file} 的状态位改成与结论一致（已收尾 / 等一句答复 / 需重开）\n"
        "  3. git add 这两个文件（外加账本 tasks/rounds.md 的历史行，账本只有一份、在工作区）"
        f"后直接 git commit，钩子把它认成「收尾提交：{round_no}」例外，不要 TASK_CENTER_TICKET\n"
        "本轮没收尾就先收尾再开新单。"
    )
    return round_no, message, None


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
    # 不给默认值：给了就没法分开「人传了阈值、要据此判红」与「没传、只看看」。理由见 --list 分支。
    parser.add_argument("--stalled-minutes", type=int, default=None,
                        help="在途单最后落库超过这么多分钟就判红（默认只统计 45 分钟，不判红）")
    args = parser.parse_args(argv[1:])
    if args.stalled_minutes is not None and args.stalled_minutes < 1:
        print("--stalled-minutes 必须是正整数（分钟）；不传就只统计不判红。", file=sys.stderr)
        return 1

    try:
        resolution = discover_db(args.db, repo=args.repo)
        db_path = resolution.db
    except center_config.DiscoveryError as error:
        resolution, db_path, db_error = None, None, error
    else:
        db_error = None
    if args.list:
        if db_path is None or not Path(db_path).is_file():
            code = db_error.code if db_error is not None else "DB_NOT_FOUND"
            detail = db_error.message if db_error is not None else f"库文件不存在：{db_path}"
            print(f"中心库没找到：{code}", file=sys.stderr)
            print(f"  {detail}", file=sys.stderr)
            print(f"  {center_config.ORDER_HINT}", file=sys.stderr)
            return 1
        if resolution is not None:
            # 凭什么读到这一份：发现顺序有多长，这一行就有多必要。
            config = f"；配置 {resolution.config}" if resolution.config is not None else ""
            print(f"中心库：{db_path}（{resolution.source}{config}）", file=sys.stderr)
        unreadable: list = []
        try:
            items = active(db_path, unreadable)
        except CenterUnreadable as error:
            # 库本身读不了就必须非零退出：零行 + 退出 0 等于宣称「没有人在做」。
            # 单单读不出来（AW-163）不进这里：那一种要「其余照常 + 这几张点名」。
            print_unreadable(error, db_path,
                             "这不是「没有活跃尝试」，是「查不出来」。修好之前不要把它当成没人干活。")
            return 1
        if not args.json:
            # 六列：前四列回答「谁该动手」，后两列回答「等了多久」。
            # `--json` 时不打表格：stdout 上一行表格一行 JSON 谁都解析不了。
            for item in items:
                print(f"{item['ticket_id']}\t{item['project_id']}\t{item['state']}\t{item['session_id'] or '(未绑定)'}"
                      f"\t{item['started_at']}\t{item['last_event_at']}")
        threshold = args.stalled_minutes if args.stalled_minutes is not None else DEFAULT_STALLED_MINUTES
        overdue = stalled(items, threshold)
        if unreadable and not args.json:
            # 不在上面的表里、但也不是「没人干活」：逐张点名，含错误码与报文。
            for entry in unreadable:
                where = (f"单 {entry['ticket_id']}" if entry["ticket_id"]
                         else f"项目 {entry['project_id']} 的全部工单")
                print(f"读不出来：{where}（{entry['project_id']}）{entry['code']}：{entry['message']}",
                      file=sys.stderr)
            print("  这几张是「读不出来」，不是「没人在做」；其余在途单已照常列在上面。"
                  "一张单读不出来不该让整份清单空掉，但也不能就此当它不存在。", file=sys.stderr)
        # 汇总行与判红都走 stderr：stdout 只留 6 列表格，机器解析不会被最后一行绊倒。
        summary = f"共 {len(items)} 张在途，其中 {len(overdue)} 张最后落库超过 {threshold} 分钟"
        if unreadable:
            summary += f"；另有 {len(unreadable)} 张读不出来，已逐条点名"
        print(summary, file=sys.stderr)
        # 传了阈值就据它判红；没传只统计。理由是同一份清单在两个场合用：巡检要一条
        # 能变红的命令（传阈值），而开工前查「有没有人在做」不该因为别人在跑就跑不成——
        # 后者一旦恒为 1，钩子与日常流程会把它当噪声跳过。
        failing = bool(overdue) and args.stalled_minutes is not None
        if failing:
            names = "、".join(f"{item['ticket_id']}（{item['last_event_at']}）" for item in overdue)
            print(f"疑似停住：{names}", file=sys.stderr)
            print(f"  判据：最后事件时间距今超过 {threshold} 分钟。要么去问它一句，"
                  "要么 recover_ticket 换一张再跑；不要看着不动当它在做。", file=sys.stderr)
        try:
            snapshot = project_snapshot(db_path, args.repo, unreadable)
        except CenterUnreadable as error:
            # 清单已经打印出来了，但「在途集合长什么样」「本轮收了没有」判不了：同样非零。
            print_unreadable(error, db_path, "清单打印出来了，但判不了在途集合与本轮收没收。")
            return 1
        # 上面那张表数的是「有活跃尝试的单」，这一行数的是「在途集合」——收尾闸门与
        # 收尾提交例外按同一个集合判，所以两条命令不会再给出相反的两句话。
        if snapshot is not None:
            print(describe_in_flight(snapshot), file=sys.stderr)
            if snapshot["waiting"]:
                # 直接点名：这一句就是「说没事却提交不了」的那个缺口（AW-094）。
                print(f"没人在做，在等人领走：{'、'.join(snapshot['waiting'])}", file=sys.stderr)
                print("  它们是 ready / rework，都算在途：收尾闸门会拦，收尾提交的钩子也会拦。"
                      "没打算现在做就先 cancel_ticket 或在库里排成 planned。", file=sys.stderr)
        round_no, unclosed, round_note = None, None, None
        try:
            round_no, unclosed, round_note = unclosed_round(snapshot, args.repo, db_path)
        except CenterUnreadable as error:
            # 轮次属于别的项目时要读那个项目的现场；读不了同样非零，不把「判不了」当「收过了」。
            print_unreadable(error, db_path,
                             "判不出这一轮收没收；修好之后再跑一次 --list，不要当成收过了。")
            return 1
        if round_note and not args.json:
            print(round_note, file=sys.stderr)
        pool_lines, pool_stale = ([], [])
        if snapshot is not None:
            # 池子明细逐张打印（不是只数张数）：念头与写好的单要分得出来，排久了要看得见。
            pool_lines, pool_stale = describe_pool(snapshot)
            for line in pool_lines:
                print(line, file=sys.stderr)
        # 年龄线只管「报出来」这一件事：报在哪里、谁去看。它是同一张池子从「可见」
        # 逆成「长草」的分界线，所以不能只写一行提示而退出 0，也不能只进 JSON 而不说。
        pool_stale_detail = None
        if pool_stale:
            listed = "、".join(f"{item['ticket_id']}（{item['days']:.1f} 天）" for item in pool_stale)
            pool_stale_detail = (
                f"pool-stale：池子里 {len(pool_stale)} 张 planned 排够 {PLANNED_MAX_DAYS} 天还没开工"
                f"（{listed}）；条件补齐了就 claim_ticket，写不出条件就 revise_ticket 或 cancel_ticket"
            )
            if not args.json:
                print(pool_stale_detail, file=sys.stderr)
        if unclosed is not None:
            print(unclosed, file=sys.stderr)
            print(f"本轮 {round_no} 没有收尾：先收尾再开新单（判据与落点见 docs/closing.md）。", file=sys.stderr)
        # 退出码先定下来，再打印：JSON 里的 ok 必须与它一致。以前 JSON 先打印、
        # 「本轮未收尾」在后面单独 return 1，于是同一份回执会一边写 ok=true 一边退 1。
        failures: list[str] = []
        if failing:
            failures.append(
                f"stalled：{len(overdue)} 张在途单最后落库超过 {threshold} 分钟"
                f"（{'、'.join(item['ticket_id'] for item in overdue)}）"
            )
        if snapshot is not None and snapshot["waiting"]:
            # ready / rework 是「人领走」的活：没人做、但收尾会拦。以前只有钩子知道，
            # 于是 --list 说没事、提交却连扫三次（AW-094）。现在这里就点名。
            failures.append(
                f"in-flight：{len(snapshot['waiting'])} 张在途单在等人领走"
                f"（{'、'.join(snapshot['waiting'])}）；收尾闸门与收尾提交都会因此拦下这一轮"
            )
        if unclosed is not None:
            failures.append(f"unclosed-round：本轮 {round_no} 没有收尾，先收尾再开新单")
        if unreadable:
            # 读不出来的单必须能被判成失败：它们没进 items，不点名就等于把「查不清」当成了「没事」。
            failures.append(
                f"unreadable：{len(unreadable)} 张单/项目读不出来"
                f"（{'、'.join(entry['ticket_id'] or entry['project_id'] for entry in unreadable)}）；"
                "上面那张表不含它们，别按它判「没人在做」"
            )
        if pool_stale_detail:
            failures.append(pool_stale_detail)
        if args.json:
            result = {
                "items": items, "count": len(items),
                "stalled_count": len(overdue), "stalled_minutes": threshold,
                "stalled": [item["ticket_id"] for item in overdue],
            }
            if unreadable:
                result["unreadable"] = unreadable
            if snapshot is not None:
                result["in_flight"] = snapshot
            if round_note:
                result["round_note"] = round_note
            if failures:
                # 失败时必须写清为什么，不然调用方只能看到一个 false。
                result["reason"] = "；".join(failures)
            print(json.dumps({"ok": not failures, "result": result}, ensure_ascii=False, indent=2))
        return 1 if failures else 0
    # 收尾提交判定必须排在「没有工单号就拒」之前：收尾包本来就不该有工单能背书。
    closing = closing_commit(args.repo, db_path)
    if closing["verdict"] == "allow":
        if args.json:
            print(json.dumps({"ok": True, "result": {"mode": closing.get("mode", "closing"),
                                                      "round": closing["round"],
                                                      "detail": closing["detail"]}}, ensure_ascii=False, indent=2))
        elif closing.get("mode") == "packet":
            # 收尾包改动走的是工单，不是收尾这条例外：说清依据，别让它看起来像特权。
            print(closing["detail"])
            print("  放行理由：暂存区只含收尾包（可带 round.md 的状态行），状态位没有改成三档之一，"
                  "所以它不是收尾；而本项目的工单已把当前尝试落到 running 并绑定了会话，由它背书。")
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
