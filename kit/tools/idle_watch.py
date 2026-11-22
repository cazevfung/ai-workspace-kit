#!/usr/bin/env python
"""悬空扫描：我在等你一句话，而你并不知道。

为什么要有这个脚本：工单体系管得住「有单在跑」的会话，管不住主任务。主任务里
最常见的一种悬空是——我抛出一个问题、一张提问卡，然后这一轮结束；主人没看见，
会话就静静躺在那里几小时。这件事以前只能靠人翻会话列表发现，或者靠巡检碰运气。

判据只有一条：**最后一个说话的人是谁**。

- 末条是我、且是在问（问句或提问卡），之后没有主人说话 → 我在等 → 等待时长
  达到 `--wait-minutes`（默认 30）记 `WAIT`，达到 `--stalled-minutes`（默认 120）
  另标 `STALLED`。
- 末条是我、但不是问句 → 这一轮收口了，`OK`，不报。
- 末条是主人、之后没有我的话 → 我该动了没动，记 `OWNER_SPOKE_NO_REPLY`，
  与上面同样按等待时长分级、同样计入 WAIT。
- 末条是别的会话 `send_to_session` 打进来的消息 → **不算主人答复**，不看它，
  继续往前找到真正的最后一句人话再判。

主人 vs 同伴的判法：把 `messages.agent_meta` 当 JSON 解出来，**只看顶部键**。
顶部有 `origin` 或 `agent-request` 的是别的会话打进来的（协调会话派单、同伴回执），
判同伴、不算答复；顶部没有这两个键而有 `sourceDevice` 的才是主人说的；其余（空、
`{}`、解不开的老数据）当未知，按「不是主人答复」处理。

**不能用子串包含判**（以前就是 `'"sourceDevice"' in raw`）。消息的正文也装在这份
元数据里，正文提到同名键就能把它骗到：`a7e4ca66` 里主人手打的那条，顶部只有
`sourceDevice`，正文里却出现了 `agent-request` 字样，子串判会把它算成同伴说的——
主人的话被当成别人代发的，方向正好相反。顶部键优先于正文内容。

单子 vs 主任务：`attempts.session_id`、`attempts.coordinator_session_id` 与
`reviews.reviewer_session_id` 三者的并集里的会话算「单子侧」。验收会话两个尝试字
段都不占，不并进来就会落进「没绑过工单的 = 主任务」那个桶，于是验收完了、单也关
了的一条会话照样能把闸门永久点亮。绑在**仍在跑**的尝试上的会话不报——那种沉默归工单流程管，
报进来只会淹没真正没人管的主任务；绑在已终结尝试上、但工单还没关的会话照报，
kind 记 `ticket_side`，因为那时它已经退回成一条普通会话了。

两类历史默认不报（`--include-history` 才列出来，标 `HISTORICAL`，且**都不计入
退出码**）：

- `closed_ticket`：绑的工单状态已是 `done` / `cancelled`。那种会话早已交差，
  末条问句没人回也不再需要谁去管。
- `stale_window`：等待时长超过 `--since-hours`（默认 24 小时）。算的是**等待时长**
  （末条人话到现在），不是会话创建时间——三天前建、五分钟前刚问的会话仍该报。

**跳过也要打出来。** 末行固定写出「关单历史 N 条」「超窗口 M 条」，即使全部被
跳过、一条都不报也一样。静默丢弃会让闸门看起来永远绿或永远红，两种都不能用。

只读：会话库照 `tools/session_roster.py` 的写法用 `mode=ro` 加 `PRAGMA query_only=ON`
打开，一行都不写；工单库同样只读。缺库是错误（`DB_NOT_FOUND`），不是「没有悬空」。

    python tools/idle_watch.py                       # 人读：只列 WAIT 行 + 末行合计
    python tools/idle_watch.py --json                # 结构化结果
    python tools/idle_watch.py --wait-minutes 60     # 换阈值
    python tools/idle_watch.py --since-hours 72      # 换历史窗口（默认 24）
    python tools/idle_watch.py --include-history     # 连关单/超窗口的一并列出
    python tools/idle_watch.py --workdir D:/x --workdir D:/y
    python tools/idle_watch.py --sessions-db <cindy 库> --db <工单库>

退出码：0 = 没有 WAIT；1 = 有 WAIT；2 = 参数或库有问题（原因写 stderr，
缺库打印 `DB_NOT_FOUND`）。缺席与「没有悬空」必须分开，否则这个工具白做。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / ".data" / "task-center.sqlite3"
# 默认扫安装时的那份工作区：install.py 会把占位换成真实安装路径，所以在装好的
# 工作区里它就是那个绝对目录；在套件仓库里直接跑时占位没被替换，扫到 0 个会话
# （不是错，是没指定要扫哪儿）——要看别的目录就重复传 --workdir。
# 以前这里硬编码了两台本机目录，换成别人 clone 下来一个都扫不到。
DEFAULT_WORKDIRS = ("{{WORKSPACE}}",)
# 用户级指针：装好的工作区把中心库位置写在这里（与 work_gate 读的是同一份）。
CONFIG = Path.home() / ".agents" / "center.json"

# 问句识别：末尾的问号，或下面这些「我在等你表态」的说法。
# 放成模块常量，测试能直接打到（不靠复述一份正则）。
QUESTION_MARKS = ("？", "?")
QUESTION_MARKERS = (
    "要不要",
    "等你",
    "你定",
    "你说一句",
    "回我一句",
    "回一句",
    "需要你",
    "你说了算",
    "待你",
)

# 参与「最后一句人话」判定的角色。thinking / tool_use / tool_result 是过程，
# 不是人话：只看它们会把我自己干活时的中间叙述当成末条。
CONVERSATIONAL_ROLES = ("assistant", "user", "ask_user")
ASSISTANT_SIDE_ROLES = ("assistant", "ask_user")

# 尝试还活着 = 这张单的沉默归工单流程管，不在这里报。
ACTIVE_ATTEMPT_STATES = ("dispatching", "running", "submitted", "blocked")

WAIT = "WAIT"
STALLED = "STALLED"
OWNER_SPOKE_NO_REPLY = "OWNER_SPOKE_NO_REPLY"
MAIN_TASK = "main_task"
TICKET_SIDE = "ticket_side"
HISTORICAL = "HISTORICAL"

# 工单已经关了：绑在它上面的会话不再需要谁去回，默认不报。
CLOSED_TICKET_STATES = ("done", "cancelled")

# 两类历史的标签：末行按这两个名字分开数条数。
HISTORY_CLOSED = "closed_ticket"
HISTORY_STALE = "stale_window"

# 默认历史窗口（小时）：比这更久的等待标 HISTORICAL。算的是等待时长，不是会话创建时间。
DEFAULT_SINCE_HOURS = 24.0

# 一次只看最后几条人话就够了：末条、以及它前面那几条同伴消息。
TAIL_MESSAGES = 5


class WatchError(RuntimeError):
    """查不出结论时的唯一异常：带一句人能读的原因，由 main 打到 stderr。"""


# ---------------------------------------------------------------- 时间与文本


def now_ms() -> int:
    return int(time.time() * 1000)


def as_millis(value) -> int:
    """库里的时间戳统一成毫秒。

    现场是毫秒整数；万一哪天换成秒，`10**11` 这条界线能把 2026 年以前的时间
    分出来，不至于把 195 分钟算成 195 毫秒。
    """
    number = int(value or 0)
    if number and number < 10**11:
        number *= 1000
    return number


def message_text(raw) -> str:
    """`messages.content` 是 JSON 字符串：dict 取 `text`，普通字符串直接用。"""
    if raw is None:
        return ""
    text = raw if isinstance(raw, str) else str(raw)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return text
    if isinstance(parsed, dict):
        for key in ("text", "content", "message"):
            value = parsed.get(key)
            if isinstance(value, str):
                return value
        return ""
    if isinstance(parsed, str):
        return parsed
    return text


def is_question(text: str) -> bool:
    """末句是不是在问：末尾问号，或含「等你表态」的说法。"""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped.endswith(QUESTION_MARKS):
        return True
    return any(marker in stripped for marker in QUESTION_MARKERS)


def meta_kind(agent_meta) -> str:
    """一条 user 消息是谁说的：`owner` / `peer` / `unknown`。

    把元数据当 JSON 解出来，**只看顶部键**：

    - 顶部有 `origin` 或 `agent-request` → `peer`（别的会话打进来的：协调会话派
      单、同伴回执），两者与 `sourceDevice` 同时出现时也以它为准；
    - 否则顶部有 `sourceDevice` → `owner`（主人本人）；
    - 其余（空串、`{}`、解不开的、不是对象的）→ `unknown`，按「不是主人答复」
      处理——宁可多报一次悬空，也不要把它当成已回。

    **不用子串包含判**（以前是 `'"sourceDevice"' in raw`）。消息的正文也装在这份
    元数据里，正文提到同名键就能骗到它：`a7e4ca66` 里主人手打的那条顶部只有
    `sourceDevice`、正文里出现了 `agent-request` 字样，子串判会判成 `peer`，把
    主人的话当成同伴说的。只看顶部键，正文写什么都不影响结论。
    """
    raw = agent_meta or ""
    if not isinstance(raw, str):
        raw = str(raw)
    if not raw.strip():
        return "unknown"
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return "unknown"
    if not isinstance(parsed, dict):
        return "unknown"
    if "origin" in parsed or "agent-request" in parsed:
        return "peer"
    if "sourceDevice" in parsed:
        return "owner"
    return "unknown"


# ---------------------------------------------------------------- 库


def read_only(path) -> sqlite3.Connection:
    """只读打开：`mode=ro` + `PRAGMA query_only`，与 session_roster 同一写法。"""
    path = Path(path)
    if not path.is_file():
        raise WatchError(f"DB_NOT_FOUND：库不存在：{path}")
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.Error as exc:
        raise WatchError(f"DB_NOT_FOUND：打不开库 {path}（{exc}）") from exc
    return conn


def default_ticket_db() -> Path:
    """默认工单库：本检出的 `.data/task-center.sqlite3` → 用户级指针 → 主检出。

    装好的工作区里第一项就命中。在套件仓库里（`kit/` 下没有任何库）时第一项不存在，
    但 `~/.agents/center.json` 指的那份库是同一个唯一真相，所以照它读，不另建一个：
    巡检读错库报出来的「没有待办」比报不出来更坏。工作树里按 git 公共目录回到主检出。
    """
    if DEFAULT_DB.is_file():
        return DEFAULT_DB
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
    try:
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        configured = Path(str(config.get("db") or "")).expanduser()
        if configured.is_file():
            return configured
    except (OSError, ValueError):
        pass
    return DEFAULT_DB


def placeholder_workdirs(workdirs) -> list[str]:
    """还没被安装脚本替换掉的占位目录（`{{...}}` 形式）。"""
    return [str(item) for item in workdirs if "{{" in str(item) and "}}" in str(item)]


def cindy_home() -> Path:
    override = os.environ.get("CINDY_HOME")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Cindy"
    return Path.home() / "AppData" / "Roaming" / "Cindy"


def default_sessions_db(home: Path | None = None) -> Path | None:
    """默认会话库：`cindy-*.db` 里 mtime 最新的那个（排除备份与 WAL/SHM）。"""
    home = Path(home) if home else cindy_home()
    if not home.is_dir():
        return None
    candidates = []
    for path in home.glob("cindy-*.db"):
        name = path.name.lower()
        if ".bak" in name or name.endswith("-shm") or name.endswith("-wal"):
            continue
        if not path.is_file():
            continue
        candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime)


def ticket_states(conn: sqlite3.Connection) -> dict[str, str]:
    """工单号 → 工单状态。

    `tickets` 表不在（老库、只造 attempts 的夹具）就返回空：这时状态算「未知」，
    按未关单处理——宁可多报一次悬空，也不要把一条真悬空当成历史静默丢掉。
    """
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tickets'"
    ).fetchone()
    if table is None:
        return {}
    try:
        rows = conn.execute("SELECT id, state FROM tickets").fetchall()
    except sqlite3.Error:
        return {}
    return {row["id"]: (row["state"] or "") for row in rows}


def bound_sessions(conn: sqlite3.Connection) -> dict[str, dict]:
    """会话标识 → {"alive": 是否仍绑在活着的尝试上, "closed": 是否只绑在已关的单上}。

    三个来源的并集：执行会话（`session_id`）、协调会话（`coordinator_session_id`）
    与验收会话（`reviews.reviewer_session_id`）。同一条会话可能出现在多条尝试上，
    只要有一条还活着就算活着。

    验收会话只按工单状态判关单、不看尝试是否活着：它不是执行会话，它的沉默本来
    就不归工单流程管——工单还在 `awaiting_review` / `rework` 时，验收会话等人的那
    句照样要报。并进来之后，「绑单已 done / cancelled 默认不报」这条规则自动盖到
    它身上，不用另写一套静音逻辑。

    `closed` 取「这条会话所有能查到状态的工单都已关单」：一条会话同时挂在一张
    已关单和一张状态未知的单上时，它仍然算没关——那种沉默还是要人看。
    """
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='attempts'"
    ).fetchone()
    if table is None:
        raise WatchError("SCHEMA_MISMATCH：工单库里没有 attempts 表，无法区分单子与主任务")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(attempts)")}
    states = ticket_states(conn)
    select = "SELECT session_id, coordinator_session_id, state"
    select += ", ticket_id FROM attempts" if "ticket_id" in columns else \
        ", NULL AS ticket_id FROM attempts"
    marks: dict[str, dict] = {}
    try:
        rows = conn.execute(select).fetchall()
    except sqlite3.Error as exc:
        raise WatchError(f"SCHEMA_MISMATCH：读不了 attempts 表（{exc}）") from exc
    for row in rows:
        alive = (row["state"] or "") in ACTIVE_ATTEMPT_STATES
        state = states.get(row["ticket_id"]) if row["ticket_id"] else None
        for key in ("session_id", "coordinator_session_id"):
            value = row[key]
            if not value:
                continue
            mark = marks.setdefault(value, {"alive": False, "states": []})
            mark["alive"] = mark["alive"] or alive
            if state:
                mark["states"].append(state)
    for row in reviewer_attempts(conn, columns):
        state = states.get(row["ticket_id"]) if row["ticket_id"] else None
        mark = marks.setdefault(
            row["reviewer_session_id"], {"alive": False, "states": []}
        )
        if state:
            mark["states"].append(state)
    return {
        key: {
            "alive": mark["alive"],
            "closed": bool(mark["states"])
            and all(state in CLOSED_TICKET_STATES for state in mark["states"]),
        }
        for key, mark in marks.items()
    }


def reviewer_attempts(conn: sqlite3.Connection, attempt_columns) -> list[sqlite3.Row]:
    """验收会话 → 它验的那张单的工单号。

    挂法：`reviews.reviewer_session_id` → `reviews.submission_id` = `submissions.id`
    → `submissions.attempt_id` = `attempts.id` → `attempts.ticket_id` → `tickets.state`。
    验收会话既不是执行会话也不是协调会话，不并进单子侧就会被当成主任务。

    缺 `reviews` / `submissions` 表（老库、只造 attempts 的夹具）就没有这一路来源，
    返回空数组；`attempts` 没有 `ticket_id` 列时同样拿不到工单号，两种都按「状态
    未知」处理——默认不报历史，也不把一条真悬空静默丢掉。
    """
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"reviews", "submissions"}.issubset(names):
        return []
    ticket_column = "a.ticket_id" if "ticket_id" in attempt_columns else \
        "NULL AS ticket_id"
    try:
        return conn.execute(
            "SELECT r.reviewer_session_id AS reviewer_session_id,"
            f" {ticket_column}"
            " FROM reviews r"
            " JOIN submissions s ON s.id = r.submission_id"
            " JOIN attempts a ON a.id = s.attempt_id"
        ).fetchall()
    except sqlite3.Error:
        return []


def normalize_workdir(value) -> str:
    """路径比较口径：反斜杠归一、去尾斜杠、大小写不分（Windows）。"""

    text = str(value or "").strip().replace("\\", "/").rstrip("/")
    return text.casefold()


def scanned_sessions(conn: sqlite3.Connection, workdirs) -> list[sqlite3.Row]:
    wanted = {normalize_workdir(item) for item in workdirs}
    try:
        rows = conn.execute(
            "SELECT id, title, working_dir, status, hidden_at"
            " FROM sessions"
            " WHERE hidden_at IS NULL AND (status IS NULL OR status != 'archived')"
        ).fetchall()
    except sqlite3.Error as exc:
        raise WatchError(f"SCHEMA_MISMATCH：读不了 sessions 表（{exc}）") from exc
    return [row for row in rows if normalize_workdir(row["working_dir"]) in wanted]


def tail_messages(conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in CONVERSATIONAL_ROLES)
    try:
        return conn.execute(
            "SELECT role, content, created_at, agent_meta, rewind_at"
            " FROM messages"
            f" WHERE session_id=? AND rewind_at IS NULL AND role IN ({placeholders})"
            " ORDER BY created_at DESC, rowid DESC"
            f" LIMIT {TAIL_MESSAGES}",
            (session_id, *CONVERSATIONAL_ROLES),
        ).fetchall()
    except sqlite3.Error as exc:
        raise WatchError(f"SCHEMA_MISMATCH：读不了 messages 表（{exc}）") from exc


# ---------------------------------------------------------------- 判定


def judge(tail: list[sqlite3.Row], at_ms: int, wait_minutes: float, stalled_minutes: float):
    """看一个会话的最后几条人话，返回记录或 None。

    「不是主人说的话」不算答复：从末尾往前跳过连续的同伴/未知 user 消息，
    停在第一条真正算数的人话上。
    """
    index = 0
    while index < len(tail) and tail[index]["role"] == "user":
        if meta_kind(tail[index]["agent_meta"]) == "owner":
            break
        index += 1
    if index >= len(tail):
        return None
    last = tail[index]
    role = last["role"]
    at = as_millis(last["created_at"])
    minutes = round(max(0.0, (at_ms - at) / 60000.0), 1)

    if role == "ask_user":
        reason = "ask_card"
    elif role == "assistant":
        if not is_question(message_text(last["content"])):
            return None  # 末条是我、但不是问句：这一轮收口了
        reason = "assistant_question"
    else:  # 主人说了话，之后没有我
        reason = "owner_no_reply"

    if minutes < wait_minutes:
        return None
    return {
        "message_at": at,
        "waiting_minutes": minutes,
        "reason": reason,
        "stalled": minutes >= stalled_minutes,
    }


def scan(sessions_conn: sqlite3.Connection, ticket_conn: sqlite3.Connection,
         workdirs, wait_minutes: float = 30, stalled_minutes: float = 120,
         at_ms: int | None = None, since_hours: float = DEFAULT_SINCE_HOURS,
         include_history: bool = False) -> dict:
    """扫一遍，返回结构化结果（人读输出与 --json 都从这里出）。

    `summary["wait"]` 只数**进了退出码**的条目。历史条目（关单历史、超窗口）
    无论 `include_history` 打不打出来，都只进 `summary["historical"]` 与两个
    `skipped_*` 计数，不进 `wait`——否则 `--include-history` 会让闸门重新变红。
    """
    at_ms = now_ms() if at_ms is None else int(at_ms)
    bound = bound_sessions(ticket_conn)
    window_minutes = max(0.0, float(since_hours)) * 60.0
    records = []
    skipped_active = 0
    skipped_closed = 0
    skipped_stale = 0
    scanned = 0

    for row in scanned_sessions(sessions_conn, workdirs):
        scanned += 1
        session_id = row["id"]
        mark = bound.get(session_id) or {}
        if mark.get("alive"):
            skipped_active += 1  # 单子还在跑：它的沉默归工单流程
            continue
        tail = tail_messages(sessions_conn, session_id)
        if not tail:
            continue
        verdict = judge(tail, at_ms, wait_minutes, stalled_minutes)
        if verdict is None:
            continue
        history_kind = None
        if mark.get("closed"):
            history_kind = HISTORY_CLOSED  # 单已关：这条沉默不再需要人回
            skipped_closed += 1
        elif verdict["waiting_minutes"] > window_minutes:
            history_kind = HISTORY_STALE  # 等太久了：先当成账，不再当成待办
            skipped_stale += 1
        verdict["historical"] = history_kind is not None
        verdict["history_kind"] = history_kind
        verdict["session_id"] = session_id
        verdict["title"] = row["title"] or ""
        verdict["kind"] = TICKET_SIDE if session_id in bound else MAIN_TASK
        if verdict["historical"] and not include_history:
            continue
        records.append(verdict)

    records.sort(key=lambda item: (-item["waiting_minutes"], item["session_id"]))
    waiting = [item for item in records]
    actionable = [item for item in records if not item["historical"]]
    return {
        "generated_at": datetime.datetime.fromtimestamp(
            at_ms / 1000, tz=datetime.timezone.utc
        ).isoformat(),
        "workdirs": list(workdirs),
        "wait_minutes": wait_minutes,
        "stalled_minutes": stalled_minutes,
        "since_hours": since_hours,
        "include_history": include_history,
        "scanned": scanned,
        "waiting": waiting,
        "summary": {
            "wait": len(actionable),
            "wait_minutes": sum(1 for item in actionable if item["waiting_minutes"] >= wait_minutes),
            "stalled": sum(1 for item in actionable if item["stalled"]),
            "main_task": sum(1 for item in actionable if item["kind"] == MAIN_TASK),
            "ticket_side": sum(1 for item in actionable if item["kind"] == TICKET_SIDE),
            "skipped_active_ticket": skipped_active,
            "skipped_closed_ticket": skipped_closed,
            "skipped_stale": skipped_stale,
            "historical": len(records) - len(actionable),
        },
    }


# ---------------------------------------------------------------- 输出


def human_line(item: dict) -> str:
    """`WAIT 195min main_task <36 位 id> <标题> [STALLED] [OWNER_SPOKE_NO_REPLY]`。

    历史条目换成 `HISTORICAL closed_ticket 6000min ...`，一眼能看出它不进退出码。
    会话标识原样整段打印：截断或拼接的 id 拿去搜是搜不到的。
    """
    if item.get("historical"):
        head = f"{HISTORICAL} {item.get('history_kind') or HISTORY_STALE}"
    elif item["stalled"]:
        head = f"{WAIT} {STALLED}"
    else:
        head = WAIT
    minutes = item["waiting_minutes"]
    minutes_text = f"{int(minutes)}min" if float(minutes).is_integer() else f"{minutes}min"
    fields = [head, minutes_text, item["kind"], item["session_id"], item["title"]]
    if item["reason"] == "owner_no_reply":
        fields.append(OWNER_SPOKE_NO_REPLY)
    elif item["reason"] == "ask_card":
        fields.append("[ASK_CARD]")
    return " ".join(fields).rstrip()


def summary_line(result: dict) -> str:
    summary = result["summary"]
    wait = result["wait_minutes"]
    stalled = result["stalled_minutes"]
    wait_text = f"{int(wait)}" if float(wait).is_integer() else f"{wait}"
    stalled_text = f"{int(stalled)}" if float(stalled).is_integer() else f"{stalled}"
    line = (
        f"合计：扫描 {result['scanned']} 个会话；WAIT {summary['wait']} 条"
        f"（≥{wait_text} 分钟 {summary['wait_minutes']} 条，"
        f"≥{stalled_text} 分钟 {summary['stalled']} 条）；"
        f"主任务 {summary['main_task']} 条，单子侧 {summary['ticket_side']} 条；"
        f"在跑工单跳过 {summary['skipped_active_ticket']} 条；"
        f"关单历史 {summary['skipped_closed_ticket']} 条；"
        f"超窗口 {summary['skipped_stale']} 条"
    )
    # 列出历史条目时说明一句：否则「WAIT 0 条」与满屏 HISTORICAL 行看起来像打架。
    if result.get("include_history") and summary["historical"]:
        line += f"（--include-history 已列出 {summary['historical']} 条历史）"
    return line


def render_human(result: dict) -> str:
    lines = [human_line(item) for item in result["waiting"]]
    lines.append(summary_line(result))
    return "\n".join(lines)


# ---------------------------------------------------------------- 入口


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="idle_watch.py",
        description="扫一遍会话库：谁在等我一句话而我不知道（只读）。",
        epilog="退出码：0 没有 WAIT；1 有 WAIT；2 参数或库有问题。",
    )
    parser.add_argument(
        "--workdir", action="append", default=None, metavar="PATH",
        help="要扫的项目目录，可重复；默认只扫安装时的那份工作区（{{WORKSPACE}}）",
    )
    parser.add_argument(
        "--sessions-db", default=None, metavar="PATH",
        help="Cindy 会话库；默认取 Cindy 目录下 mtime 最新的 cindy-*.db",
    )
    parser.add_argument(
        "--db", default=None, metavar="PATH",
        help=f"工单中心库（用来区分单子与主任务）；默认 {DEFAULT_DB}",
    )
    parser.add_argument("--wait-minutes", type=float, default=30.0, metavar="N",
                        help="等待多久算 WAIT（默认 30）")
    parser.add_argument("--stalled-minutes", type=float, default=120.0, metavar="N",
                        help="等待多久另标 STALLED（默认 120）")
    parser.add_argument("--since-hours", type=float, default=DEFAULT_SINCE_HOURS,
                        metavar="H",
                        help="等待超过这么多小时的历史条目标 HISTORICAL、不计入退出码"
                             "（默认 24；算的是等待时长，不是会话创建时间）")
    parser.add_argument("--include-history", action="store_true",
                        help="连关单历史与超窗口的条目一并列出（仍不计入退出码）")
    parser.add_argument("--json", action="store_true", help="输出结构化结果")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:  # Windows 控制台默认 GBK，中文与 emoji 一打印就 UnicodeEncodeError
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    try:
        args = parse_args(sys.argv[1:] if argv is None else list(argv))
    except SystemExit as exc:  # --help 走这里，退出码 0；参数错是 2
        return int(exc.code or 0)

    if args.wait_minutes <= 0 or args.stalled_minutes <= 0 or args.since_hours <= 0:
        print("错误：--wait-minutes / --stalled-minutes / --since-hours 必须是正数",
              file=sys.stderr)
        return 2

    sessions_path = Path(args.sessions_db) if args.sessions_db else default_sessions_db()
    if sessions_path is None:
        print(f"DB_NOT_FOUND：找不到 Cindy 会话库（在 {cindy_home()} 下没有 cindy-*.db）",
              file=sys.stderr)
        return 2
    ticket_path = Path(args.db) if args.db else default_ticket_db()
    workdirs = list(args.workdir) if args.workdir else list(DEFAULT_WORKDIRS)
    if not args.workdir:
        unresolved = placeholder_workdirs(workdirs)
        if unresolved:
            # 空着不解释会被当成「没有悬空」：占位没被 install.py 换掉，一条会话都匹配不上。
            print(
                "提示：默认工作区还是占位 "
                + "、".join(unresolved)
                + "（说明是在套件仓库里直接跑，不是装好的工作区）；"
                "这一次一个会话都扫不到，要看哪个目录就用 --workdir 指明。",
                file=sys.stderr,
            )

    sessions_conn = ticket_conn = None
    try:
        sessions_conn = read_only(sessions_path)
        ticket_conn = read_only(ticket_path)
        result = scan(sessions_conn, ticket_conn, workdirs,
                      wait_minutes=args.wait_minutes,
                      stalled_minutes=args.stalled_minutes,
                      since_hours=args.since_hours,
                      include_history=args.include_history)
    except WatchError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        for conn in (sessions_conn, ticket_conn):
            if conn is not None:
                conn.close()

    result["sessions_db"] = str(sessions_path)
    result["db"] = str(ticket_path)
    if args.json:
        payload = {key: value for key, value in result.items()}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_human(result))
    return 1 if result["summary"]["wait"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
