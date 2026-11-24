#!/usr/bin/env python
"""无人值守派单的前置检查：读中心库算下一步该做什么，只读，不出手。

为什么要有这个脚本：库里排着 planned 单、有验收完没合的单、有等验收的单，这些
事以前靠协调会话记得去看。做成定时任务之后有两个问题必须当场解决，而且它们都
不是「记得更勤」能解决的：

1. **空转要钱。** 池子是空的、在途单还在跑的时候，定时任务醒来一次就要读一遍
   上下文、开一轮对话。所以「有没有活」这件事必须由一个不看脸的脚本回答：它算
   不出下一步就退出 2，调度侧据此不启动对话。**判据是退出码，不是输出里的一句话。**
2. **故障不许变成「没活」。** 库读不开、库版本不对、项目归属查不出——这些是
   不知道，不是空闲。两者混在一起最糟：调度侧会安安静静地不干活，而现场是坏
   的。所以本脚本的退出码与同目录的 `wait_audit.py` 相反：`wait_audit` 用 2 表示
   「库有问题」，这里 2 表示「没活」，**出错一律 1**（`PRD/PRD-010` §4.3 的那套
   阈值与本脚本无关：这里没有任何时间阈值，判据全在库里的状态上）。

四个结果，一次只给一个（互斥）：

    dispatch_execute  池子里有可领的单 —— 派执行会话
    dispatch_review   有单在等验收、且还没有验收会话在飞 —— 派验收会话
    merge             有已验收未合并的   —— 合并
    fix_pool          池子里只剩躺了很久的空判据单 —— 点名它，人补判据或取消
    nothing           以上都没有 / 有在途单挡着 / 暂停中 / 验收在飞

**`fix_pool` 属于「有活」而不是「没活」。** 在这之前，池子里只剩空判据的 `planned`
单时，它只是 `skipped` 里的一行、`action` 仍是 `nothing`——而那种单永远领不走
（`claim_ticket` 报 `CRITERIA_NOT_JUDGEABLE`），于是「池子里有活、只是没人能领」
被报成了「没活」。现在只要这种单的 `created_at` 已经超过
`BLANK_CRITERIA_GRACE_SECONDS`（一天），它就单独成一档被点名。**阈值只有那一个来源**，
本脚本与文档都不许各写一个数。

**它是「只报不改」的最后一档。** 两条出路都要人或协调会话动手：`revise_ticket` 补上
至少一条可判定条件，或 `cancel_ticket` 承认不做了。本脚本既不写判据也不取消单——
库仍然是 `mode=ro`，一行都不写；替人做决定会让「这张单为什么没了」无迹可查。

**报过之后要退避，报到第 N 轮要升级。** 这一档「有活」的代价是「没人处理就每轮再报
一次」：同一张单、同一句话，一天下来烧掉几十轮，而输出一字不变。现在每一次点名都要先
落一条 `record_pool_report` 事件（`task_center` 的同名写操作，或走
`tools/pool_report.py`；本脚本只读），读侧按这张单已经报过几次决定三件事：

- **还没报过 → 原样点名**（第 1 轮）；
- **报过、但还没到下一次的时间 → 这一轮不点名**（`reason_code=blank_criteria_backoff`，
  退出 2）。退避阶梓是 `POOL_REPORT_BACKOFF_SECONDS`：第 1 次之后 2 小时、第 2 次之后
  6 小时、第 3 次之后 24 小时，此后一直 24 小时；
- **报到第 `POOL_REPORT_ESCALATE_AFTER` 轮 → 升级**（`reason_code=blank_criteria_escalate`）：
  不再原样重报，人话换成「建一张 planned 单把这件事变成可验收的活，或用 ask_ticket 落一个
  问题」，两条老出路（`revise_ticket` / `cancel_ticket`）照旧。

**「报过几次」只存在库里，不靠会话记忆。** 一条 `record_pool_report` 事件 = 这张单被
点名一次；换一个会话、换一台机器读同一个库，算出来的轮次与下一次时间一模一样。
本脚本不写这条记录（它只读），所以空判据档的人话里会把要落的记录名写出来：不落，
下一轮就会从头再报一遍。

**优先级：验收 > 合并 > 派执行 > 补池子。** 前三个的理由不是偏好：`awaiting_review` 在
`DISPATCH_BUSY` 的口径里算在途（`task_center/core.py` 的 `ACTIVE_TICKET_STATES`），
它不在这个循环里被挪走，池子里的单一张也领不出来——所以它是唯一会卡住整条链的
动作，先做它。合并不挡任何派发，晚一轮没关系。

**验收派出去之后不再重复派（AW-133）。** 只看「在等验收 + 没有 `reviews` 行」是不够的：
验收会话要跑几分钟才落结论，在这段窗口里库与几轮之前一模一样，于是同一张单会
被连续几轮派第二个、第三个验收会话。所以派验收时要先落一条 `review_dispatched`
事件（`task_center` 的同名写操作），本脚本见到它就**不再**把这单算成 `dispatch_review`，
改把人读那一句写成「验收在飞」——`review_in_flight` 数组里带单号、提交与已经等了
多久。窗口是 `task_center.core.REVIEW_IN_FLIGHT_SECONDS`（30 分钟）：超时还没有
`reviews` 行就自动回落成可派，因为验收会话可能根本没起来。

**它只作「已派」提示，不改另外三档的顺位。** `review_dispatched` 不进 `BLOCKING_STATES`：
池子里有可领单、或另有已验收待合的单，该给还是照给。

**与「在途」的关系。** 挡住派发的不是 `ACTIVE_TICKET_STATES` 的全部四个状态，而是
去掉 `awaiting_review` 之后的三个：

    dispatching  已领取、还没绑定执行会话（下一步是 bind_session，不是再领一张）
    running      执行会话在跑
    blocked      开了工在等一句话（PRD-005）

`awaiting_review` 被排除，因为它自己的下一步就是「派验收」——把它当「忙」，这个
循环就永远转不动。这一处不是把在途集合改小：`DISPATCH_BUSY` 一行没动，本脚本
也不替它做判断，只是把「这一轮我能自己往前走吗」问清楚。

**合并候选的两条限制，都写在输出里，不悄悄丢掉。**

- 只认「能拿出来合的东西」：最新提交记了分支头（`submissions.head_sha`），或者仓库里还有
  `ticket/<单号>` 分支（分支不在、也没有分支头 → 库里与 git 里都没有可合的树）。
- 已有 `merges` 行的单不算未合并。

**「历史直做」这一层与 `tools/auto_merge.py --list` 同源，不在这里另判一套。**
两件工具对同一现场给矛盾的下一步，是 2026-11-23 当晚真实发生过三次的事
（`AW-139`、`AWK-113`、`AWK-106`：单已经 `done`、`merges` 里没有行，`auto_merge --list`
把它列进「真待合并」，本脚本却照旧报 `dispatch_execute` 去派新单，每次都要人或巡检
把它抳回来）。根因不是顺位，是判据少了一层——本脚本只看了 `head_sha` 一个事实。
所以豁免清单（`<项目根>/docs/unmerged-exemptions.json`，`tools/backfill_merges.py --apply`
写的）的读法、`ticket/<单号>` 分支在不在的判法，全部直接调 `auto_merge` 那几个函数：
重写一遍就等于给同一现场留两个答案。两类不算待合并的单进 `skipped` 并带单号：

    exempted       已在豁免清单里（backfill 判过它们没有可核对的证据）
    no_branch_head 没有分支头、也没有 ticket/<单号> 分支（清单里也没有它 → 快照该刷新了）

**这一个路径只读**：不 `claim`、不写库、不改工单状态、也不自动补合并记录——补记录
只有 `tools/auto_merge.py --record-only` 一个入口（`PRD-013` R10）。

**一个没有任何提交的 `done` 单不进 `skipped`，进 `stop`**：那是数据不自洽（验收
必须基于一次提交），不是「历史遗留」，得当故障报出来。

用法（在仓库根 {{WORKSPACE}}）：

    python tools/next_action.py                  # 人读：一句话
    python tools/next_action.py --json           # 机器读：结构化结果 + human 字段
    python tools/next_action.py --project ai-workspace
    python tools/next_action.py --pause-file <路径>   # 换暂停标记的位置

退出码：**0 = 有活（四个动作之一，含 `fix_pool`）；2 = 没活（含暂停中、有在途单挡着、
全在退避窗口里）；1 = 出错**
（库不存在/读不开/版本不对/缺表、项目归属查不出、参数写错）。1 与 2 分得开，
是这个脚本存在的理由之一。

`--project` 不传时按「已登记项目里 root 是当前目录上级的那个」推断；推断不出、
或有多个同样深度的候选，都报错退出 1——项目归属不许猜（`AGENTS.md` 硬规则一）。

暂停：标记文件存在就退出 2 并说明「暂停中」，一个动作都不给。默认位置
`{{WORKSPACE}}/.data/dispatch-paused`；要临时停掉整条无人值守循环，建这个空文件
就够了（`docs/无人值守派单.md`「怎么暂停」）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 状态集合与依赖层的算法都从 task_center 里取，不在这里再写一份：
# 「可领」「未完成前置」「层数」三处口径要与 claim_ticket / list_tickets 逐字同源。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from task_center.core import (  # noqa: E402  (先补 sys.path 再导入)
    ACTIVE_TICKET_STATES,
    CLAIMABLE_STATES,
    POOL_REPORT,
    REVIEW_IN_FLIGHT_SECONDS,
    dependency_depths,
    pending_blockers,
)
from task_center.db import SCHEMA_VERSION  # noqa: E402
# 合并口径的权威在 `auto_merge --list`：豁免清单怎么读、`ticket/<单号>` 分支在不在怎么判，
# 都用它那一套函数。只调这两个函数、不改它一个字节——同一现场只能有一个答案（AWK-114）。
import auto_merge  # noqa: E402

# 四个结果的名字之外还有第五档：`fix_pool`（池子里只剩领不走的空判据单）。
DISPATCH_EXECUTE = "dispatch_execute"
DISPATCH_REVIEW = "dispatch_review"
MERGE = "merge"
FIX_POOL = "fix_pool"
NOTHING = "nothing"
ACTIONS = (DISPATCH_EXECUTE, DISPATCH_REVIEW, MERGE, FIX_POOL, NOTHING)
# 有活的那四个。`nothing` 不在里面：退出码 0 只给「这一轮有事可做」。
# `fix_pool` 在里面：它要人（或协调会话）去补判据或取消单，调度侧必须开一轮去看它——
# 报成 2 就又回到了「池子里有活、而循环安静地不动」那个现场。
WORKING_ACTIONS = (DISPATCH_EXECUTE, DISPATCH_REVIEW, MERGE, FIX_POOL)

# 空判据单「躺多久之后才值得点名」的宽限期限（与上游 AW-138 同一个数与同一个理由）：
# `planned` 允许 0 条验收条件（那是「已经知道要补什么、还写不出条件」的念头），但
# `claim_ticket` 读到空列表就报 `CRITERIA_NOT_JUDGEABLE`，所以这种单永远领不走。给它一天
# 不会把正常流程报成故障；而「躺了一整天」正是会被漏掉的现场。
#
# 调这个数只有本文件这一处：文档写的是「一个来源」，不跟着改数。
BLANK_CRITERIA_GRACE_SECONDS = 24 * 60 * 60

# 点名报过一次之后，下一轮再点到它之前要等多久。
#
# 起因：把空判据单升成一档「有活」之后，代价是「没人处理就每轮再报一次」——同一张单、
# 同一句话，每轮醒来烧一轮，既没有升级也没有退避。节奏要跟着「人可能还没看见」变慢。
#
# 形状：「第 i 次点名之后等 POOL_REPORT_BACKOFF_SECONDS[i-1] 秒」。第 1 次之后 2 小时、
# 第 2 次之后 6 小时、第 3 次之后 24 小时，此后再叫就按最后一个（24 小时）。
POOL_REPORT_BACKOFF_SECONDS = (2 * 60 * 60, 6 * 60 * 60, 24 * 60 * 60)

# 报到第几轮就升级（不再原样重报）：这一轮的人话换成「建一张 planned 单 / ask_ticket」。
# 取值是退避阶梓走完一轮的点：1 次原价 + 两次退避之后再叫一次，所以它跟
# `POOL_REPORT_BACKOFF_SECONDS` 的长度对得上；改阶梓时这里一起看。
POOL_REPORT_ESCALATE_AFTER = 3

# 挡住派发的三个状态：`ACTIVE_TICKET_STATES` 去掉 `awaiting_review`（理由见模块头）。
BLOCKING_STATES = tuple(
    state for state in ACTIVE_TICKET_STATES if state != "awaiting_review"
)

# 退出码。2 是「没活」而不是「出错」，与 wait_audit.py 相反，理由见模块头。
EXIT_WORK = 0
EXIT_ERROR = 1
EXIT_IDLE = 2

# 每个状态「停在哪一步」的一句人话。加新状态时要在这里补一句，否则会被报成
# 一句泛泛的「在途」——无人值守最不需要的就是这种话。
STEP_TEXT = {
    "dispatching": "已领取、还没绑定执行会话（下一步是 bind_session）",
    "running": "执行会话在跑",
    "blocked": "开了工在等一句话（ask_ticket 落了问题）",
}

REQUIRED_TABLES = (
    "projects", "tickets", "attempts", "submissions", "reviews", "merges",
    "notices", "ticket_links", "questions", "events",
)

# 「验收已经派出去了」这件事在 `events` 里的 operation 名，与 `task_center` 的那个写操作同名。
# 同一个名字两种行都会出现：成功那次带 `result`，被拒那次带 `rejected`——只有前者算「已派」。
REVIEW_DISPATCHED = "review_dispatched"

# 「done 但没有合并记录」里那两类**不算待合并**的行，名字进 `skipped`，人读那一行也用它。
SKIP_NO_BRANCH_HEAD = "no_branch_head"
SKIP_EXEMPTED = "exempted"
# 历史豁免清单位置：与 `auto_merge.py --list` 同一份文件，按项目根算（工具与项目可能不在
# 同一个检出里，按工具自己的位置算就会读到别人的清单）。
EXEMPTIONS_RELPATH = Path("docs") / "unmerged-exemptions.json"


class PlannerError(RuntimeError):
    """查不出结论时的唯一异常：带一句人能读的原因，由 main 打到 stderr。"""


# ---------------------------------------------------------------- 库


def default_ticket_db() -> Path:
    """默认工单库：主检出的 `.data/task-center.sqlite3`。

    在 `.worktrees/<工单号>/` 里跑时，本树没有 `.data`（不入库），按 `ROOT/.data`
    取会报 `DB_NOT_FOUND`——那不是「没活」，是找错了库。所以先看本树，没有就按
    git 的公共目录找到主检出，读主检出那份库（全仓只有一份）。与
    `tools/wait_audit.py` 同一条找法，两个脚本查到不同的库，结论就会打架。
    """
    local = ROOT / ".data" / "task-center.sqlite3"
    if local.is_file():
        return local
    try:
        found = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
        )
        output = (found.stdout or "").strip()
        if found.returncode == 0 and output:
            common = Path(output)
            if not common.is_absolute():
                common = (ROOT / common).resolve()
            candidate = common.parent / ".data" / "task-center.sqlite3"
            if candidate.is_file():
                return candidate
    except (OSError, subprocess.SubprocessError):
        pass
    return local


def default_pause_file() -> Path:
    """默认暂停标记：放在工单库旁边（同一个 `.data/`，不入库）。

    跟着库走而不是跟着脚本走，是因为库只有一份、脚本可能在 `.worktrees/<单号>/`
    里跑；两处各认一个位置，就会出现「我建了标记它不认」。
    """
    return default_ticket_db().parent / "dispatch-paused"


def read_only(path) -> sqlite3.Connection:
    """只读打开：`mode=ro` + `PRAGMA query_only`，与 wait_audit / idle_watch 同一写法。"""
    # `as_uri()` 不接相对路径：给了相对的 --db 会抛 ValueError 而不是一句人话。
    path = Path(path).absolute()
    if not path.is_file():
        raise PlannerError(f"DB_NOT_FOUND：库不存在：{path}")
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.Error as exc:
        raise PlannerError(f"DB_UNREADABLE：打不开库 {path}（{exc}）") from exc
    return conn


def check_schema(conn, path) -> None:
    """库版本与关键表：对不上就报错，绝不当成「没活」。

    `PRAGMA user_version` 是 schema 版本的唯一来源（`task_center/db.py` 写的就是
    它）；`SCHEMA_VERSION` 从代码里取，不在这里再写一个数——加一级迁移时两处
    不同步，这个脚本就会在旧库上算出一份看着像真的结论。
    """
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error as exc:
        raise PlannerError(f"DB_UNREADABLE：读不出库版本（{exc}）") from exc
    if version != SCHEMA_VERSION:
        raise PlannerError(
            f"SCHEMA_MISMATCH：{path} 是 schema {version}，本脚本按 {SCHEMA_VERSION} 判；"
            "先跑 migrate 再派单，不要按旧库的结论派发"
        )
    present = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = [name for name in REQUIRED_TABLES if name not in present]
    if missing:
        raise PlannerError(
            f"SCHEMA_MISMATCH：{path} 缺表 {', '.join(missing)}，算不出下一步"
        )


# ---------------------------------------------------------------- 项目归属


def resolve_project(conn, project_id, cwd: Path) -> sqlite3.Row:
    """定位这一次要在哪个项目里派单：给定了就核对，没给就按路径推断。

    「推断不出就报错」比「拿第一个项目凑一个」重要：把别处的单领出来，代价比
    多问一句高一个数量级（`AGENTS.md` 硬规则一）。
    """
    rows = conn.execute("SELECT id, name, root FROM projects ORDER BY id").fetchall()
    if not rows:
        raise PlannerError("PROJECT_NOT_FOUND：库里没有登记任何项目，先 register_project")
    if project_id:
        for row in rows:
            if row["id"] == project_id:
                return row
        listed = "、".join(row["id"] for row in rows)
        raise PlannerError(f"PROJECT_NOT_FOUND：库里没有 {project_id}（已登记：{listed}）")

    resolved = Path(cwd).resolve()
    matches = []
    for row in rows:
        try:
            root = Path(row["root"]).resolve()
        except (OSError, ValueError):
            continue
        if resolved == root or resolved.is_relative_to(root):
            matches.append((len(root.parts), row))
    if not matches:
        raise PlannerError(
            f"PROJECT_NOT_FOUND：{resolved} 不在任何已登记项目的目录里，"
            "用 --project 指定项目再跑"
        )
    matches.sort(key=lambda item: item[0], reverse=True)
    if len(matches) > 1 and matches[0][0] == matches[1][0]:
        tied = "、".join(row["id"] for depth, row in matches if depth == matches[0][0])
        raise PlannerError(f"PROJECT_AMBIGUOUS：{resolved} 同时属于 {tied}，用 --project 指定")
    return matches[0][1]


# ---------------------------------------------------------------- 四类候选


def project_tickets(conn, project_id, states) -> list[sqlite3.Row]:
    marks = ",".join("?" for _ in states)
    return conn.execute(
        "SELECT * FROM tickets WHERE project_id=? AND state IN (" + marks + ")"
        " ORDER BY created_at, id",
        (project_id, *states),
    ).fetchall()


def latest_submission(conn, ticket_id):
    """该工单全部尝试里 `created_at` 最大的一条提交。

    返工之后 `current_attempt` 指向新尝试，旧提交仍在表里，所以按全部尝试取最新，
    不只看当前尝试（与 `wait_audit.latest_submission` 同一口径）。
    """
    rows = conn.execute(
        "SELECT s.* FROM submissions s JOIN attempts a ON a.id = s.attempt_id"
        " WHERE a.ticket_id=? ORDER BY s.created_at, s.id",
        (ticket_id,),
    ).fetchall()
    return rows[-1] if rows else None


def has_review(conn, submission_id) -> bool:
    row = conn.execute(
        "SELECT 1 FROM reviews WHERE submission_id=? LIMIT 1", (submission_id,)
    ).fetchone()
    return row is not None


def has_merge(conn, ticket_id) -> bool:
    row = conn.execute(
        "SELECT 1 FROM merges WHERE ticket_id=? LIMIT 1", (ticket_id,)
    ).fetchone()
    return row is not None


def _parse_time(value):
    """把库里存的时间读成带时区的 datetime；读不出来返回 None（不拿现在凑一个）。"""
    try:
        stamp = datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp


def review_dispatched_events(conn, ticket_id) -> list[dict]:
    """这张单上「验收已经派出去」的事实，按时间从新到旧。

    只认带 `result` 的事件行：同一个 operation 名也会出现在被拒的调用里（`execute()` 与
    `_record_rejection` 都写 `events`），那种行带的是 `rejected`，它说的是「想派但没记成」，
    不是「已经派出去了」。两者混在一起会让一次失败悄悄压住下一轮派发。
    """
    rows = conn.execute(
        "SELECT id, detail, created_at FROM events"
        " WHERE ticket_id=? AND operation=? ORDER BY created_at DESC, id DESC LIMIT 20",
        (ticket_id, REVIEW_DISPATCHED),
    ).fetchall()
    items = []
    for row in rows:
        try:
            detail = json.loads(row["detail"] or "{}")
        except (TypeError, ValueError):
            continue
        result = detail.get("result") if isinstance(detail, dict) else None
        if not isinstance(result, dict):
            continue
        items.append({
            "event_id": row["id"],
            "created_at": row["created_at"],
            "submission_id": result.get("submission_id"),
            "reviewer_session_id": result.get("reviewer_session_id"),
            "note": result.get("note"),
        })
    return items


def review_in_flight(conn, ticket_id, submission_id, now, stop) -> dict | None:
    """这张单的这一份提交是不是已经有一位验收人在看（AW-133）。

    两个条件同时成立才算在飞：事件指向**当前最新这份提交**、且时间在窗口内
    （`REVIEW_IN_FLIGHT_SECONDS`）。指向旧提交的事件不算——返工后的新提交还没人看过。

    时间读不出来时**按可派处理**：宁可多派一次验收，也不要因为一行脏时间把一张单
    默默押住——那会变成「什么都没发生」的僵局。
    """
    for event in review_dispatched_events(conn, ticket_id):
        if event["submission_id"] and event["submission_id"] != submission_id:
            continue
        stamp = _parse_time(event["created_at"])
        if stamp is None:
            stop.append(
                f"{ticket_id} 有一条 review_dispatched 事件的时间读不出来"
                f"（{event['created_at']!r}），按可派处理"
            )
            continue
        waited = (now - stamp).total_seconds()
        if waited < REVIEW_IN_FLIGHT_SECONDS:
            return {
                "ticket_id": ticket_id,
                "submission_id": submission_id,
                "event_id": event["event_id"],
                "dispatched_at": event["created_at"],
                "waited_seconds": int(waited),
                "expires_in_seconds": int(REVIEW_IN_FLIGHT_SECONDS - waited),
                "reviewer_session_id": event["reviewer_session_id"],
            }
    return None


def pool_report_events(conn, ticket_id) -> list[dict]:
    """这张单「已经被点名报过」的事实，按时间从新到旧。

    只认带 `result` 的事件行：同一个 operation 名也会出现在被拒的调用里（条件不合时
    `execute()` 写的那行带 `rejected`），它说的是「想记但没记成」，不是「已经报过」。
    两者混在一起，会让一次失败把下一轮的点名压住。
    """
    rows = conn.execute(
        "SELECT id, detail, created_at FROM events"
        " WHERE ticket_id=? AND operation=? ORDER BY created_at DESC, id DESC LIMIT 50",
        (ticket_id, POOL_REPORT),
    ).fetchall()
    items = []
    for row in rows:
        try:
            detail = json.loads(row["detail"] or "{}")
        except (TypeError, ValueError):
            continue
        result = detail.get("result") if isinstance(detail, dict) else None
        if not isinstance(result, dict):
            continue
        items.append({"event_id": row["id"], "created_at": row["created_at"]})
    return items


def pool_report_status(conn, ticket_id, now) -> dict:
    """这张单报过几次、这一轮该不该再报、下一次最早什么时候能再报。

    `rounds` 是「已经报过的次数」（不是「将要报第几轮」）：一条事件就是一次点名。
    退避窗口从**最后一次**那条事件算起：`rounds` 次之后等
    `POOL_REPORT_BACKOFF_SECONDS[min(rounds, len)-1]` 秒。

    时间读不出来时**按该报处理**：宁可多叫一次，也不要因为一行脏时间把一张永远领不走
    的单压住——那会变成「什么都没发生」的僵局，而这一档存在的理由就是不让人错过它。
    """
    events = pool_report_events(conn, ticket_id)
    rounds = len(events)
    status = {
        "reported_rounds": rounds,
        "last_reported_at": None,
        "backoff_seconds": None,
        "next_report_at": None,
        "seconds_until_next_report": 0,
        # 与「建单时间读不出来」（`timestamp_unreadable`）分开一个键：
        # 后者说的是「这张单多大了」，这里说的是「上一次点名是什么时候」。
        "report_timestamp_unreadable": False,
        "due": True,
    }
    if not rounds:
        return status
    status["last_reported_at"] = events[0]["created_at"]
    stamp = _parse_time(events[0]["created_at"])
    if stamp is None:
        status["report_timestamp_unreadable"] = True
        return status
    gap = POOL_REPORT_BACKOFF_SECONDS[
        min(rounds, len(POOL_REPORT_BACKOFF_SECONDS)) - 1
    ]
    next_at = stamp + datetime.timedelta(seconds=gap)
    left = int((next_at - now).total_seconds())
    status.update({
        "backoff_seconds": gap,
        "next_report_at": next_at.isoformat(),
        "seconds_until_next_report": max(left, 0),
        "due": left <= 0,
    })
    return status


def attach_pool_report(conn, entries, now) -> list[dict]:
    """给空判据那一档的每张单补上「报过几次 / 该不该再报 / 是不是该升级」。

    只读：本脚本一行都不写，「报过了」由协调会话或巡检调 `task_center` 的
    `record_pool_report` 落库（或走 `tools/pool_report.py`）——读侧只能读台账，不能自己盖章。

    `escalate` 只在「这一轮确实要报」且「这一轮就是第 N 轮」时为真：窗口内本来就不叫，
    不该在那里提升级——否则一天能看到很多次「请升级」，而那正是要消掉的噪音。
    """
    for entry in entries:
        status = pool_report_status(conn, entry["ticket_id"], now)
        status["escalate"] = bool(
            status["due"] and status["reported_rounds"] + 1 >= POOL_REPORT_ESCALATE_AFTER
        )
        entry.update(status)
    return entries


def blank_criteria(raw) -> bool:
    """这条工单的条件是不是一条都没有（`planned` 允许空，其余状态不允许）。

    `tickets.criteria` 存的是 JSON 文本。读不出来的行**不算**空条件：那是脏行，
    不要拿一个解析失败去冒充「念头」把它静默筛掉；它照旧进候选，真有问题
    到 `claim_ticket` 那一步会报出来。
    """
    if not raw:
        return True
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return False  # 读不出来是脏行，不当成「空条件」悄悄筛掉；交给后面当作可领单报出来
    return not value


def blocking_rows(conn, project_id) -> list[dict]:
    """挡住这一轮的在途单，每张带「停在哪一步」。

    工单级状态是权威（`docs/protocol.md` 的「两处都写，权威在工单级」），所以这里
    只看 `tickets.state`；尝试上的状态在 `blocked` 之外不参与判断，避免一条旧尝试
    把一张已经收尾的单报成还在跑。
    """
    items = []
    for row in project_tickets(conn, project_id, BLOCKING_STATES):
        item = {
            "ticket_id": row["id"],
            "title": row["title"],
            "state": row["state"],
            "step": STEP_TEXT.get(row["state"], f"停在 {row['state']}"),
        }
        attempt = None
        if row["current_attempt"]:
            attempt = conn.execute(
                "SELECT id, session_id, state FROM attempts WHERE id=?",
                (row["current_attempt"],),
            ).fetchone()
        if attempt is not None:
            item["attempt_id"] = attempt["id"]
            item["session_id"] = attempt["session_id"]
        if row["state"] == "blocked" and attempt is not None:
            question = conn.execute(
                "SELECT id, question FROM questions WHERE attempt_id=? AND state='open'"
                " ORDER BY asked_at DESC LIMIT 1",
                (attempt["id"],),
            ).fetchone()
            if question is not None:
                item["question_id"] = question["id"]
                item["question"] = question["question"]
                item["step"] = "开了工在等一句话：已落库的开放问题还没人答"
        items.append(item)
    return items


def review_candidates(conn, project_id, stop, in_flight=None, now=None) -> list[dict]:
    """在等验收的单。取不到提交、或已经有验收结论的，进 `stop` 而不是候选。

    验收已经派出去且还在窗口内的单进 `in_flight`，**不进**候选：同一份成果派两个验收人
    是重复劳动，一个 accept 一个 reject 还会把状态机推出两条路。它也不是 `stop`——
    没有出错，只是该等。
    """
    items = []
    in_flight = in_flight if in_flight is not None else []
    now = now or datetime.datetime.now(datetime.timezone.utc)
    for row in project_tickets(conn, project_id, ("awaiting_review",)):
        submission = latest_submission(conn, row["id"])
        if submission is None:
            stop.append(
                f"{row['id']} 状态是 awaiting_review，但库里没有任何提交——"
                "验收没有可依据的东西，需要人看这一行"
            )
            continue
        if has_review(conn, submission["id"]):
            stop.append(
                f"{row['id']} 已有验收结论（提交 {submission['id']} 有 review 行），"
                "工单状态还停在 awaiting_review——状态机慢一步，需要人把它走完"
            )
            continue
        pending = review_in_flight(conn, row["id"], submission["id"], now, stop)
        if pending is not None:
            in_flight.append(pending)
            continue
        items.append({
            "ticket_id": row["id"],
            "title": row["title"],
            "state": row["state"],
            "revision": row["revision"],
            "criteria_version": row["criteria_version"],
            "submission_id": submission["id"],
            "head_sha": submission["head_sha"],
            "base_sha": submission["base_sha"],
            "summary": submission["summary"],
        })
    return items


def default_exemptions_path(project_root) -> Path:
    """历史豁免清单在哪：跟着**项目根**走。

    与 `auto_merge.py --list` 读的是同一份文件（`docs/unmerged-exemptions.json`，由
    `tools/backfill_merges.py --apply` 写）。但那一份按工具自己的仓库根定位，本脚本按
    `projects.root` 定位：一个工具可能被从套件检出里跑，判的却是另一个项目的单。
    """
    return Path(project_root) / EXEMPTIONS_RELPATH


def ticket_branch_exists(project_root, ticket_id):
    """`ticket/<单号>` 分支还在不在；**查不了返回 None**（不知道 ≠ 没有）。

    为什么要问 git：`done` + 没有 `merges` 行 + 提交里没有分支头，依然可能是真待合并
    ——只看库里这两列会把「分支还躺着、只差记录」的单当成历史单。反过来，库里永远
    看不出「没有分支」，所以这条事实只能向 git 要。

    判法与 `auto_merge` 完全一样（它的 `exists` 就是 `git rev-parse --verify --quiet`）：
    git 不在、卡住、或目录不是仓库时它不返回 False 而是抛 `Fail`——那两件事分别是「没装」
    与「不是仓库」，一律不当成「分支不在」；这里转成 None 交给调用方当故障报出来。
    """
    try:
        return auto_merge.exists(Path(project_root), f"refs/heads/ticket/{ticket_id}")
    except auto_merge.Fail:
        return None


def merge_candidates(conn, project_id, project_root, skipped, stop) -> list[dict]:
    """已验收但库里没有合并记录的单，口径与 `auto_merge.py --list` 逐字同源。

    三档判据，每一步都只用已经存在的事实（不在这里发明新规则）：

    1. 最新提交记了 `head_sha` → 待合并。有没有分支头只有库知道，它是最好的一条证据。
    2. 没有分支头、但单号在 `<项目根>/docs/unmerged-exemptions.json` 里 → 历史豁免。
       清单是 `tools/backfill_merges.py --apply` 判出来的，本脚本不重判。
    3. 没有分支头、清单里也没有它 → 问 git `ticket/<单号>` 分支还在不在：在 → 待合并
       （分支还躺着，只差记录，`auto_merge --record-only` 就是为这种现场写的）；
       不在 → 历史直做（库里与 git 里都没有可合的树）；查不了 → 当故障进 `stop`。

    分不开的代价是具体的：2026-11-23 当晚 `AW-139` / `AWK-113` / `AWK-106` 三张已经验收
    的单被当成「下张单可以派了」，每次都要人或巡检把合并这一步抳回来。
    """
    items = []
    historical, exempted = [], []
    entries, exemptions_path, have_exemptions = auto_merge.load_exemptions(
        default_exemptions_path(project_root)
    )
    for row in project_tickets(conn, project_id, ("done",)):
        if has_merge(conn, row["id"]):
            continue
        submission = latest_submission(conn, row["id"])
        if submission is None:
            stop.append(
                f"{row['id']} 是 done，但库里没有任何提交——合并没有可合的树，需要人看"
            )
            continue
        head = submission["head_sha"]
        if not head:
            if row["id"] in entries:
                exempted.append(row["id"])
                continue
            branch = ticket_branch_exists(project_root, row["id"])
            if branch is None:
                stop.append(
                    f"{row['id']} 是 done、没有合并记录、提交里也没有分支头；"
                    f"而且查不到 ticket/{row['id']} 分支在不在（git 不可用或超时）——"
                    "分不出它是历史单还是待合并，需要人看"
                )
                continue
            if not branch:
                historical.append(row["id"])
                continue
        items.append({
            "ticket_id": row["id"],
            "title": row["title"],
            "state": row["state"],
            "head_sha": head,
            "created_at": row["created_at"],
            "submission_id": submission["id"],
            "notice_count": conn.execute(
                "SELECT COUNT(*) FROM notices WHERE ticket_id=?", (row["id"],)
            ).fetchone()[0],
        })
    if historical:
        skipped.append({
            "reason": SKIP_NO_BRANCH_HEAD,
            "detail": "done 且无合并记录，但提交里没有分支头、仓库里也没有 ticket/<单号> 分支"
                      "（合并流程之前的直接提交，库里没有可合的东西）；清单里也没有它们，"
                      "所以豁免快照该刷新了：跑 `python tools/backfill_merges.py --apply`。"
                      "要不要补记录由人决定，本脚本不代做。",
            "ticket_ids": historical,
        })
    if exempted:
        source = str(exemptions_path) if have_exemptions else f"{exemptions_path}（文件不存在）"
        skipped.append({
            "reason": SKIP_EXEMPTED,
            "detail": f"done 且无合并记录，但已在历史豁免清单里（{source}）："
                      "backfill 判过它们没有可核对的证据，补一条记录等于把没人记过写成已经合了。"
                      "它们不是等待合并的单。",
            "ticket_ids": exempted,
        })
    # 先验收的先合：同一份 master 上少积一层。排序按 created_at，与池子视图的
    # 「同层按 created_at」一个习惯，不按单号字符串比大小（AW-9 与 AW-10 会排反）。
    items.sort(key=lambda item: (item["created_at"], item["ticket_id"]))
    return items


def execute_candidates(conn, project_id, skipped, stale=None, now=None) -> list[dict]:
    """池子里可领的单：状态可领、`blocked_by` 全部 done、且写得出验收条件。

    `claimable` 的定义从 `task_center.core` 取（`CLAIMABLE_STATES` +
    `pending_blockers`），与 `list_tickets --planned` 那一列同源：这里多出来的只有
    「挑哪一张」，不重新定义什么叫可领。排序也与池子视图一致：依赖层数升序，
    同层按 `created_at`。

    **条件空着的单不算可领**（AW-109）：`planned` 允许 0 条条件，那是「已经知道要补
    什么、还写不出验收条件」的念头；`claim_ticket` 会报 `CRITERIA_NOT_JUDGEABLE`。
    不在这里筛掉的话，无人值守派单器会一圈一圈地派一张永远领不走的单。

    **躺太久的还给一份到 `stale`**：它永远是 `skipped` 里的 `no_criteria`，但只写
    这一行不够——`action` 仍是 `nothing`，于是这种单被「没活」替它报平安。所以
    `created_at` 超过 `BLANK_CRITERIA_GRACE_SECONDS` 的那些另进 `stale`，由 `scan()`
    升成 `fix_pool` 点名。阈值不在这里再写一个数：本文件只有上面那一处。时间读不出来时
    **按超期处理并标 `timestamp_unreadable`**：宁可多报一次（成本是一轮会话），
    也不要因为一行脏时间把一张永远领不走的单静默筛掉。
    """
    stale = stale if stale is not None else []
    now = now or datetime.datetime.now(datetime.timezone.utc)
    rows = project_tickets(conn, project_id, CLAIMABLE_STATES)
    depths = dependency_depths(conn, [row["id"] for row in rows])
    ordered = sorted(
        rows, key=lambda row: (depths.get(row["id"], 0), row["created_at"], row["id"])
    )
    items, waiting, blank = [], [], []
    for row in ordered:
        if blank_criteria(row["criteria"]):
            entry = {
                "ticket_id": row["id"],
                "state": row["state"],
                "created_at": row["created_at"],
            }
            blank.append(entry)
            stamp = _parse_time(row["created_at"])
            if stamp is None:
                stale.append({
                    **entry,
                    "age_seconds": None,
                    "timestamp_unreadable": True,
                })
                continue
            age = (now - stamp).total_seconds()
            if age >= BLANK_CRITERIA_GRACE_SECONDS:
                stale.append({**entry, "age_seconds": int(age),
                              "timestamp_unreadable": False})
            continue
        pending = pending_blockers(conn, row["id"])
        if pending:
            waiting.append({
                "ticket_id": row["id"],
                "blocked_by": [{"ticket_id": item["id"], "state": item["state"]}
                               for item in pending],
            })
            continue
        items.append({
            "ticket_id": row["id"],
            "title": row["title"],
            "state": row["state"],
            "revision": row["revision"],
            "criteria_version": row["criteria_version"],
            "prd_path": row["prd_path"],
            "prd_sha256": row["prd_sha256"],
            "depth": depths.get(row["id"], 0),
        })
    if waiting:
        skipped.append({
            "reason": "blocked_by_pending",
            "detail": "可领状态但有前置单没 done（只有 done 算满足，cancelled 也算没满足）",
            "tickets": waiting,
        })
    if blank:
        skipped.append({
            "reason": "no_criteria",
            "detail": ("池子里的念头：条件还空着，领走也验收不了（claim_ticket 会报 "
                       "CRITERIA_NOT_JUDGEABLE）；先 revise_ticket 补至少一条可判定条件。"
                       "其中躺过一天的会升成 fix_pool 单列一格（补判据 / cancel_ticket）"),
            "tickets": blank,
        })
    return items


# ---------------------------------------------------------------- 算下一步


def scan(conn, project_id, pause_path=None, cwd=None, now=None) -> dict:
    """扫一遍，返回结构化结果（人读输出与 --json 都从这里出）。

    只有一个时间阈值：**验收在飞**的 30 分钟窗口（`REVIEW_IN_FLIGHT_SECONDS`）。它必须
    存在，否则「已派出去」这句话永远不会失效；所以 `now` 可以显式传入（用例靠它把
    事件摆到 31 分钟前），不传就是此刻。其余四个结果全靠库里的状态与依赖边算出来，
    同一个库同一个 `now` 跑两次结论必然一样（`generated_at` 除外）。
    """
    if conn.row_factory is not sqlite3.Row:
        # 调用方拿到的是普通连接（夹具、嵌入式使用）时补上，下面的 row["列名"] 才成立。
        conn.row_factory = sqlite3.Row
    project = resolve_project(conn, project_id, Path(cwd or Path.cwd()))
    pid = project["id"]
    current = now or datetime.datetime.now(datetime.timezone.utc)
    stop: list[str] = []
    skipped: list[dict] = []
    result = {
        "schema": 1,
        "generated_at": _now(),
        "project_id": pid,
        "project_root": project["root"],
        "action": NOTHING,
        "reason_code": "",
        "human": "",
        "ticket": None,
        "dispatch": None,
        "in_flight": [],
        "review_in_flight": [],
        "fix_pool": [],
        "stop": stop,
        "skipped": skipped,
        # 退避与升级的口径从常量里原样拓印出来：人读输出、用例与文档都不必再写一个数。
        "pool_report": {
            "operation": POOL_REPORT,
            "backoff_seconds": list(POOL_REPORT_BACKOFF_SECONDS),
            "escalate_after": POOL_REPORT_ESCALATE_AFTER,
        },
        "candidates": {DISPATCH_REVIEW: [], MERGE: [], DISPATCH_EXECUTE: [], FIX_POOL: []},
    }

    marker = Path(pause_path) if pause_path else default_pause_file()
    if marker.is_file():
        result["reason_code"] = "paused"
        result["human"] = f"nothing：暂停中——{marker} 存在，一个动作都不给"
        result["pause_file"] = str(marker)
        return result

    blocking = blocking_rows(conn, pid)
    if blocking:
        result["in_flight"] = blocking
        names = "、".join(f"{item['ticket_id']}（{item['state']}：{item['step']}）"
                         for item in blocking)
        result["reason_code"] = "in_flight"
        result["human"] = f"nothing：有在途单挡着——{names}；等它收尾再派下一张"
        for item in blocking:
            stop.append(
                f"{item['ticket_id']} 停在 {item['state']}：{item['step']}"
            )
        return result

    reviews = review_candidates(conn, pid, stop, result["review_in_flight"], current)
    # 合并候选先算、也先给：「done 但没合并」的单一旦被「池子里还有可领单」遮住，这一轮
    # 就会去开下一张单，而上一张的成果还躺在分支上（AWK-114 的三次现场）。
    merges = merge_candidates(conn, pid, project["root"], skipped, stop)
    executes = execute_candidates(conn, pid, skipped, result["fix_pool"], current)
    # 空判据那一档先管「躺得够久就点名」，再补上「报过几次、该不该再报、是不是该升级」
    # ——只读台账，不写。
    if result["fix_pool"]:
        attach_pool_report(conn, result["fix_pool"], current)
    result["candidates"] = {
        DISPATCH_REVIEW: [item["ticket_id"] for item in reviews],
        MERGE: [item["ticket_id"] for item in merges],
        DISPATCH_EXECUTE: [item["ticket_id"] for item in executes],
        FIX_POOL: [item["ticket_id"] for item in result["fix_pool"]],
    }

    if reviews:
        pick = reviews[0]
        result["action"] = DISPATCH_REVIEW
        result["reason_code"] = "awaiting_review"
        result["ticket"] = _ticket_head(conn, pick["ticket_id"])
        result["dispatch"] = {
            "ticket_id": pick["ticket_id"],
            "attempt_id": _current_attempt(conn, pick["ticket_id"]),
            "submission_id": pick["submission_id"],
            "head_sha": pick["head_sha"],
            "base_sha": pick["base_sha"],
            "revision": pick["revision"],
            "criteria_version": pick["criteria_version"],
            "summary": pick["summary"],
        }
        result["human"] = (
            f"{DISPATCH_REVIEW}：派验收 {pick['ticket_id']}"
            f"（提交 {pick['submission_id']}，head {_short(pick['head_sha'])}）"
        )
        return _finish(result)

    if merges:
        pick = merges[0]
        result["action"] = MERGE
        result["reason_code"] = "done_not_merged"
        result["ticket"] = _ticket_head(conn, pick["ticket_id"])
        result["dispatch"] = {
            "ticket_id": pick["ticket_id"],
            "submission_id": pick["submission_id"],
            "head_sha": pick["head_sha"],
            "notice_count": pick["notice_count"],
        }
        if pick["notice_count"] == 0:
            stop.append(
                f"{pick['ticket_id']} 的 notices 为空：auto_merge 第 2 步会拒，"
                "先由协调会话 record_notice 再合并"
            )
        if not pick["head_sha"]:
            # 没有分支头也是待合并（分支还躺着），但验收过的那棵树记不下来：
            # auto_merge 第 2 步只警告不拦，记录按 `unverified` 落库。这句要说出来。
            stop.append(
                f"{pick['ticket_id']} 的提交里没有分支头：auto_merge 第 2 步只能警告，"
                "合并记录会按 unverified 落库（被验收的那棵树无法核对）"
            )
        result["human"] = (
            f"{MERGE}：合并 {pick['ticket_id']}"
            f"（已验收未合并，head {_short(pick['head_sha'])}）"
        )
        if len(merges) > 1:
            result["human"] += f"；另有 {len(merges) - 1} 张也已验收未合并：" + "、".join(
                item["ticket_id"] for item in merges[1:]
            )
        return _finish(result)

    if executes:
        pick = executes[0]
        result["action"] = DISPATCH_EXECUTE
        result["reason_code"] = "claimable"
        result["ticket"] = _ticket_head(conn, pick["ticket_id"])
        result["dispatch"] = {
            "ticket_id": pick["ticket_id"],
            "state": pick["state"],
            "revision": pick["revision"],
            "criteria_version": pick["criteria_version"],
            "prd_path": pick["prd_path"],
            "prd_sha256": pick["prd_sha256"],
            "depth": pick["depth"],
        }
        basis = pick["prd_path"] or "（未登记 PRD 路径）"
        result["human"] = (
            f"{DISPATCH_EXECUTE}：派执行 {pick['ticket_id']}"
            f"（{pick['state']}，依据 {basis}）"
        )
        return _finish(result)

    if stop:
        # 一个能自动做的动作都没有，但库里有不自洽的行：说「等人看」，不说「没活」。
        result["reason_code"] = "needs_human"
        result["human"] = "nothing：没有能自动做的动作，需要人看——" + "；".join(stop)
        return _finish(result)

    if result["review_in_flight"]:
        # 验收派出去了、结论还没落库：这一轮什么也不派，但要说清在等谁、还等多久。
        # 它不叫 `in_flight`：那个词说的是「有单挡着派发」，这里只是等一个已经开跑的动作。
        waiting = "；".join(
            f"{item['ticket_id']}（提交 {item['submission_id']}，"
            f"已等 {_waited_text(item['waited_seconds'])}）"
            for item in result["review_in_flight"]
        )
        minutes = REVIEW_IN_FLIGHT_SECONDS // 60
        result["reason_code"] = "review_in_flight"
        result["human"] = (
            f"nothing：验收已经派出去、结论还没落库——{waiting}；"
            f"不再重复派验收，{minutes} 分钟没有 reviews 行就自动回落成可派"
        )
        return _finish(result)

    if result["fix_pool"]:
        # 池子里只剩「永远领不走」的单。它排在三档派发动作之后、`nothing` 之前：
        # 有真能往前走的动作时那个优先，但一旦只剩它们，就不许再说「没活」——
        # 池子里明明有单，只是没人能领。两条出路都要人动手，本脚本一个都不替它做。
        #
        # 这里还要问两件事：这一轮该不该再叫（退避）、叫到第几轮了（升级）。
        # 两者都只从 `record_pool_report` 台账算，不看会话记忆。
        due = [item for item in result["fix_pool"] if item["due"]]
        if not due:
            # 全在退避窗口里：这一轮不点名，退出 2（调度侧不开会话）。这不是「静默」：
            # 每一张单都已经被点名过至少一次，名字、轮次与下一次时间都在 `fix_pool`
            # 数组里，人跑一次 `--json` 就看得到；不落库记录才是真静默。
            waiting = "；".join(
                f"{item['ticket_id']}（已报 {item['reported_rounds']} 轮，"
                f"还剩 {_span_text(item['seconds_until_next_report'])}）"
                for item in result["fix_pool"]
            )
            result["reason_code"] = "blank_criteria_backoff"
            result["human"] = (
                f"nothing：{len(result['fix_pool'])} 张空判据单都还在退避窗口里——{waiting}；"
                "到点之前不再点名（点名记录在库里，换一个会话也读得到），"
                "要提前处理就直接 revise_ticket 补判据或 cancel_ticket"
            )
            return _finish(result)
        escalating = [item for item in due if item["escalate"]]
        if escalating:
            # 报到第 N 轮：换一种叫法。仍然只报不改——建单 / 提问由读它的会话做，
            # 本脚本不写判据、不取消单、也不替你开单。
            pick = escalating[0]
            result["action"] = FIX_POOL
            result["reason_code"] = "blank_criteria_escalate"
            result["ticket"] = _ticket_head(conn, pick["ticket_id"])
            result["dispatch"] = {
                "ticket_id": pick["ticket_id"],
                "ticket_ids": [item["ticket_id"] for item in due],
                "grace_seconds": BLANK_CRITERIA_GRACE_SECONDS,
                # 两条合法出路，都落库、都查得到是谁为什么：补判据 / 承认不做了。
                "outcomes": ["revise_ticket", "cancel_ticket"],
                "escalate": True,
                "reported_rounds": {
                    item["ticket_id"]: item["reported_rounds"] for item in due
                },
                "escalate_after": POOL_REPORT_ESCALATE_AFTER,
                # 升级的两条路，与「不许同步提问卡」不冲突：`ask_ticket` 是落库的问题，
                # 不是卡片。
                "escalation": {
                    "ticket_ids": [item["ticket_id"] for item in escalating],
                    "options": ["create_ticket:planned", "ask_ticket"],
                },
                "report_operation": POOL_REPORT,
            }
            names = "、".join(
                f"{item['ticket_id']}（已连着报过 {item['reported_rounds']} 轮）"
                for item in escalating
            )
            result["human"] = (
                f"{FIX_POOL}：{names}，没人处理（升级门槛是 {POOL_REPORT_ESCALATE_AFTER} 轮）——"
                "这一轮不再原样重报，升级成一件能验收的活：用 create_ticket 建一张 "
                "state=planned 的单（把「补判据 / 取消这几张空判据单」写成可验收条件），"
                "或在本会话持有 running 尝试时用 ask_ticket 落一个问题；"
                "两条老出路仍是 revise_ticket / cancel_ticket。"
                "next_action 只报不改，不会替你开单、写判据或取消单；"
                f"这一轮也要落一条 {POOL_REPORT} 记录，否则下一轮还会再喊一次升级"
            )
            return _finish(_note_backoff(result, due))
        stale = due
        names = "、".join(
            f"{item['ticket_id']}（{item['state']}，{_age_text(item)}，{_rounds_text(item)}）"
            for item in stale
        )
        result["action"] = FIX_POOL
        result["reason_code"] = "blank_criteria"
        result["ticket"] = _ticket_head(conn, stale[0]["ticket_id"])
        result["dispatch"] = {
            "ticket_id": stale[0]["ticket_id"],
            "ticket_ids": [item["ticket_id"] for item in stale],
            "grace_seconds": BLANK_CRITERIA_GRACE_SECONDS,
            # 两条合法出路，都落库、都查得到是谁为什么：补判据 / 承认不做了。
            "outcomes": ["revise_ticket", "cancel_ticket"],
            "escalate": False,
            "reported_rounds": {
                item["ticket_id"]: item["reported_rounds"] for item in stale
            },
            "report_operation": POOL_REPORT,
        }
        result["human"] = (
            f"{FIX_POOL}：池子里 {len(stale)} 张单的验收条件空着、已经躺过 "
            f"{_grace_text()}——{names}；它们永远领不走（claim_ticket 报 "
            "CRITERIA_NOT_JUDGEABLE），两条出路：用 revise_ticket 补至少一条可判定条件，"
            "或用 cancel_ticket 承认不做了。next_action 只报不改，不会替你写判据、也不会替你取消；"
            f"点名之后要落一条 {POOL_REPORT} 记录"
            "（python tools/pool_report.py --project <项目 id> --ticket <单号>），"
            "否则下一轮还会从第 1 轮重报"
        )
        return _finish(_note_backoff(result, due))

    result["reason_code"] = "idle"
    result["human"] = "nothing：没活——池子里没有可领的单，也没有待验收、待合并的单"
    return _finish(result)


def _note_backoff(result: dict, due: list[dict]) -> dict:
    """同一档里有的到点、有的还在退避：人读那一行补一句，别让后一半消失。"""
    waiting = len(result.get("fix_pool") or []) - len(due)
    if waiting:
        result["human"] += f"；另有 {waiting} 张空判据单还在退避窗口里，到点再点"
    return result


def _finish(result: dict) -> dict:
    """收口：把 `skipped` 里那些「看得见但不派动作」的行缀进人话。

    为什么非要缀在人话里：一次 `done` 无合并记录的单如果没有分支头，它不是没活，
    而是**没有可合的东西**——不派 `merge` 是对的，但让这句结论悄悄消失就说不清了
    （`AGENTS.md` 与 `auto_merge.py --list` 都把这类单摆出来让人判断，这里不能替人
    隐去）。结构化结果里已经是完整的行，人读那一行只补一句「另有 N 张，为什么」。
    """
    notes = []
    # 只有在这一格不是「等验收」本身时才补这句：`nothing / review_in_flight` 的那句话
    # 已经把单号与等待时间写全了，再缀一次就是同一件事说两遍。
    if result.get("reason_code") != "review_in_flight":
        for item in result.get("review_in_flight") or []:
            notes.append(
                f"另有 {item['ticket_id']} 的验收已经派出去（提交 {item['submission_id']}，"
                f"已等 {_waited_text(item['waited_seconds'])}），不再重复派"
            )
    for item in result.get("skipped") or []:
        ids = item.get("ticket_ids") or []
        if not ids:
            continue
        if item.get("reason") == SKIP_NO_BRANCH_HEAD:
            notes.append(
                f"另有 {len(ids)} 张 done 且无合并记录，但提交里没有分支头、仓库里也没有"
                f"ticket/<单号> 分支（合并流程之前的直接提交，见 skipped）：{'、'.join(ids)}"
            )
        elif item.get("reason") == SKIP_EXEMPTED:
            notes.append(
                f"另有 {len(ids)} 张 done 且无合并记录，但已在历史豁免清单里"
                f"（不是等待合并的单，见 skipped）：{'、'.join(ids)}"
            )
    # 已经单列成 `fix_pool` 那一档时不重复：那句话已经把单号与两条出路写全了。
    # 全在退避窗口里的那一档也已经有自己的一句话（带轮次与还剩多久），不再缀一遍。
    if result.get("action") != FIX_POOL and result.get("reason_code") != "blank_criteria_backoff":
        stale = result.get("fix_pool") or []
        if stale:
            notes.append(
                f"另有 {len(stale)} 张空判据的单已经躺过 {_grace_text()}"
                f"（{', '.join(item['ticket_id'] + '，' + _rounds_text(item) for item in stale)}，"
                "见 fix_pool）：补判据（revise_ticket）或 cancel_ticket，"
                "否则它们永远领不走"
            )
    if notes:
        result["human"] = result["human"] + "；" + "；".join(notes)
    return result


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _waited_text(seconds: int) -> str:
    """等待时长的人话：不满一分钟说「1 分钟以内」，别写「已等 0 分钟」——那读起来像没在等。"""
    if seconds < 60:
        return "1 分钟以内"
    return f"{seconds // 60} 分钟"


def _grace_text() -> str:
    """空判据的期限人话：按天算，不把 86400 秒这种数写进人话。

    取值只从 `BLANK_CRITERIA_GRACE_SECONDS` 推算，不在这里再写一个 24。
    """
    days = BLANK_CRITERIA_GRACE_SECONDS / 86400
    return f"{days:.0f} 天" if days >= 1 else f"{BLANK_CRITERIA_GRACE_SECONDS // 60} 分钟"


def _rounds_text(item: dict) -> str:
    """这张单已经被点名报过几次的人话。只从台账算，不猜。"""
    rounds = item.get("reported_rounds") or 0
    if not rounds:
        return "还没报过"
    return f"已报过 {rounds} 轮"


def _span_text(seconds: int) -> str:
    """剩余时长的人话：不足一分钟不说「0 分钟」，那读起来像已经到点。"""
    seconds = max(int(seconds), 0)
    if seconds < 60:
        return "1 分钟以内"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    hours, rest = divmod(seconds, 3600)
    if hours < 24:
        return f"{hours} 小时" + (f" {rest // 60} 分钟" if rest >= 60 else "")
    days, rest = divmod(hours, 24)
    return f"{days} 天" + (f" {rest} 小时" if rest else "")


def _age_text(item: dict) -> str:
    """一张空判据单已经躺了多久的人话；时间读不出来时就说读不出来，不编一个天数。"""
    if item.get("timestamp_unreadable") or item.get("age_seconds") is None:
        return "创建时间读不出来，按超期算"
    seconds = item["age_seconds"]
    if seconds < 86400:
        return f"已经 {max(seconds // 3600, 1)} 小时"
    return f"已经 {seconds // 86400} 天"


def _short(sha) -> str:
    text = (sha or "").strip()
    return text[:8] if text else "无"


def _ticket_head(conn, ticket_id) -> dict:
    row = conn.execute(
        "SELECT id, title, state, revision, criteria_version, prd_path"
        " FROM tickets WHERE id=?", (ticket_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def _current_attempt(conn, ticket_id):
    row = conn.execute(
        "SELECT current_attempt FROM tickets WHERE id=?", (ticket_id,)
    ).fetchone()
    return row["current_attempt"] if row is not None else None


# ---------------------------------------------------------------- 输出与入口


def exit_code(action: str) -> int:
    """有活 0，没活 2。`nothing` 一律 2——包括「有在途单挡着」与「需要人看」：
    这两种调度侧都不该再开一轮对话，区别写在 `reason_code` 与 `stop` 里。"""
    return EXIT_WORK if action in WORKING_ACTIONS else EXIT_IDLE


def render_json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="next_action.py",
        description="读中心库算出无人值守的下一步动作（只读）。",
        epilog="退出码：0 有活；2 没活（含暂停中、有在途单挡着）；1 出错。",
    )
    parser.add_argument(
        "--db", default=None, metavar="PATH",
        help=f"工单中心库；默认 {default_ticket_db()}",
    )
    parser.add_argument(
        "--project", default=None, metavar="ID",
        help="项目 id；不传时按当前目录在哪个已登记项目里推断（推断不出就报错）",
    )
    parser.add_argument(
        "--pause-file", default=None, metavar="PATH",
        help="暂停标记文件；默认放在工单库旁边（`.data/dispatch-paused`），存在即退出 2 并说明暂停中",
    )
    parser.add_argument("--json", action="store_true", help="输出结构化结果")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:  # Windows 控制台默认 GBK，中文一打印就 UnicodeEncodeError
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    try:
        args = parse_args(sys.argv[1:] if argv is None else list(argv))
    except SystemExit as exc:  # --help 走这里，退出码 0；参数错是 2，改成 1 免得被当成没活
        code = int(exc.code or 0)
        return code if code == 0 else EXIT_ERROR

    path = Path(args.db) if args.db else default_ticket_db()
    conn = None
    try:
        conn = read_only(path)
        check_schema(conn, path)
        result = scan(conn, args.project, pause_path=args.pause_file)
    except PlannerError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    except sqlite3.Error as exc:
        print(f"DB_UNREADABLE：读库失败（{exc}）", file=sys.stderr)
        return EXIT_ERROR
    finally:
        if conn is not None:
            conn.close()

    if args.json:
        print(render_json(result))
    else:
        print(result["human"])
    return exit_code(result["action"])


if __name__ == "__main__":
    sys.exit(main())
