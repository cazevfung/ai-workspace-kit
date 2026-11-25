#!/usr/bin/env python
"""回执丢了没人醒：扫一遍「谁在等谁」，只读，不写一行。

为什么要有这个脚本：协调会话派完验收之后，以前会留在自己这一轮里 sleep 等回执，
睡一会儿查一次库。实测同一条会话日志里有 31 次 sleep（sleep 1700 四次、sleep 1800
三次、sleep 1500 三次），动作都是「睡完再查一次库」。这占着一条活轮：期间不能纠错、
不能响应排队消息，睡过头就是盲等。

正确的做法是 dispatch 完就结束本轮，唯一续跑点是回执到达时起的新一轮。但那条路有个
前提必须补上——**回执本身不保证送达**：`review_result` / `done` / 合并都只落库不外发，
唯一的外发是评审会话自己发的一条回执，那是礼貌不是保证；`send_to_session` 是入队语义，
还可能被宿主当成新任务误建。所以「回执没到」必须有一个不看会话脸的检出面，这个脚本
就是那个面：它直接查库，报出五类链条停住的地方。

五类（判据与阈值都来自 PRD-010，不在这里发明新数）：

1. `waiting_review`：工单 `state='awaiting_review'`，最新一条提交在 `reviews` 里没有行，
   且提交到现在的时长超过 4 小时。出处：`PRD/PRD-010-项目进度面板.md` §4.3 S3。
   已经有 review 行、只是工单状态还没翻的，按「已有 review」不算——那是状态机慢一步，
   不是没人看。
2. `awaiting_review_no_notice`：工单 `state='awaiting_review'`，最新一条提交存在，
   但该工单 `notices` 一行都没有，且提交到现在的时长超过 20 分钟。出处：
   `PRD/PRD-010-项目进度面板.md` §4.3 S3a——4 小时那档的分钟级前哨。为什么是 20：
   巡检每 10 分钟一轮，20 分钟 = 两轮都没人接；提交通常是执行会话这一轮的最后一步，
   两轮之内没人接手基本就是这条链断了，多出的那一轮是留给回执自然补发的机会。
   这里判的是「这张单一条回执记录都没有」，不是「最新提交之后没有」——`notices` 是按
   工单走的，时间窗只用 `submissions.created_at`；已经有任意一行（含 `failed`）就不再报，
   那与 `merge_ticket` 的 `NO_NOTICE` 是同一条尺：回执走过就不再是「没发出去」。
3. `done_no_notice`：工单 `state='done'` 但 `notices` 里一行都没有。闭环没走完，
   事后查不出回执到底发没发。
4. `done_not_merged`：工单 `state='done'` 但 `merges` 里没有行。已验收但没合。
   注意这一类比 PRD-010 §4.4 的「真待合并」宽：它不查分支，历史直做的单也会命中。
   这里报的是「库里没合并记录」这个事实，分「真待合并 / 历史直做」属于 §4.4 的分类，
   不在只读巡检里做。要那一层分类看面板或 `tools/auto_merge.py --list`。
   **`archived` 不在五类里，也不需要在这里特判**：扫 `state='done'` 的是上面第 3、4
   两档（`ticket_rows`），扫 `attempts.state='running'` 的是第 5 档，而归档单既不是 `done` 也不再跑（AW-128）。所以「清账之后
   这两类归零」靠的是状态真的变了，不是把阈值调松——归档单的 `reviews` / `submissions`
   仍在库里，想核「它到底验收过没有」查 `get_ticket` 就够。
5. `running_idle`：`attempts.state='running'`，但它绑的执行会话已经超过 10 分钟没有活动。
   出处：`PRD/PRD-010-项目进度面板.md` §4.3 S4 的 10 分钟阈值。S4 原文判的是「会话悬着」，
   这里按工单方向用同一个 10 分钟：单还说在跑、执行会话却静了，就是派出去没人管的现场。
   会话正在跑这一轮（`active_turn_started_at` 非空且晚于 `last_turn_ended_at`）不算——
   长命令跑十分钟不等于卡住（§4.2「会话在跑」判据）；`last_activity_at` 为空的不参与判定，
   与 §4.3 的反例一致。

只读：两个库都用 `mode=ro` 加 `PRAGMA query_only=ON` 打开，一行都不写；缺库是错误
（`DB_NOT_FOUND`），不是「没有问题」。缺席与「没有待办」必须分开，否则这个工具白做。

    python tools/wait_audit.py                          # 人读：命中行 + 末行五类合计
    python tools/wait_audit.py --json                   # 结构化结果
    python tools/wait_audit.py --minutes 30             # 换「久无活动」阈值（默认 10）
    python tools/wait_audit.py --sessions-db <cindy 库> --db <工单库>

退出码：0 = 五类都为空；1 = 有命中（新档也算命中）；2 = 参数或库有问题（原因写 stderr，
缺库打印 `DB_NOT_FOUND`）。末行固定打印五类各几条，即使全零也一样——静默丢弃会让闸门
看起来永远绿或永远红，两种都不能用。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / ".data" / "task-center.sqlite3"

# 阈值只在模块常量里写一次，旁边标出处；测试与方法文档都打到这里，不各写一份。
# 4 小时：PRD-010 §4.3 S3「待验收超时」。
AWAITING_REVIEW_HOURS = 4.0
# 10 分钟：PRD-010 §4.3 S4「会话悬着」。
SESSION_IDLE_MINUTES = 10.0
# 20 分钟：PRD-010 §4.3 S3a「待验收且没有回执」，4 小时那档的分钟级前哨。
# 为什么正好是 20：巡检每 10 分钟一轮，两轮 = 一个巡检周期乘 2——给回执一次自然
# 补发的机会，第二次还没动静才点名。阈值只在代码里定义这一次，PRD-010 §4.3 S3a
# 与 docs/protocol.md 的 `wait_audit` 一段写的是同一个口径与出处。
AWAITING_REVIEW_NO_NOTICE_MINUTES = 20.0

# 五类的名字：人读输出、--json、末行合计都用这五个，不另起别名。
WAITING_REVIEW = "waiting_review"
AWAITING_REVIEW_NO_NOTICE = "awaiting_review_no_notice"
DONE_NO_NOTICE = "done_no_notice"
DONE_NOT_MERGED = "done_not_merged"
RUNNING_IDLE = "running_idle"

KINDS = (
    WAITING_REVIEW,
    AWAITING_REVIEW_NO_NOTICE,
    DONE_NO_NOTICE,
    DONE_NOT_MERGED,
    RUNNING_IDLE,
)

# 末行合计的中文名：顺序与 KINDS 一致。
KIND_LABELS = {
    WAITING_REVIEW: "等在验收",
    AWAITING_REVIEW_NO_NOTICE: "待验收没回执",
    DONE_NO_NOTICE: "done 没回执",
    DONE_NOT_MERGED: "done 没合并",
    RUNNING_IDLE: "running 久无活动",
}


def default_ticket_db() -> Path:
    """默认工单库：主检出的 `.data/task-center.sqlite3`。

    在 `.worktrees/<工单号>/` 里跑时，脚本所在的树没有 `.data`（不入库），直接按
    `ROOT/.data` 取会报 `DB_NOT_FOUND`——那不是「没有待办」，是找错了库。所以先看
    本树，没有就按 git 的公共目录找到主检出，读主检出那份库（全仓只有一份）。
    两条都不成立时返回本树路径，让 `read_only` 照常报 `DB_NOT_FOUND`。
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


class AuditError(RuntimeError):
    """查不出结论时的唯一异常：带一句人能读的原因，由 main 打到 stderr。"""


# ---------------------------------------------------------------- 库与时间


def read_only(path) -> sqlite3.Connection:
    """只读打开：`mode=ro` + `PRAGMA query_only`，与 idle_watch / session_roster 同一写法。"""
    # `as_uri()` 不接相对路径：给了相对的 --db 会抛 ValueError 而不是一句人话。
    path = Path(path).absolute()
    if not path.is_file():
        raise AuditError(f"DB_NOT_FOUND：库不存在：{path}")
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.Error as exc:
        raise AuditError(f"DB_NOT_FOUND：打不开库 {path}（{exc}）") from exc
    return conn


def cindy_home() -> Path:
    override = os.environ.get("CINDY_HOME")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Cindy"
    return Path.home() / "AppData" / "Roaming" / "Cindy"


def default_sessions_db(home: Path | None = None) -> Path | None:
    """默认会话库：`cindy-*.db` 里 mtime 最新的那个（排除备份与 WAL/SHM）。

    与 `tools/idle_watch.py` 用同一条找法：两个脚本查到不同的库，报出来的结论就会打架。
    """
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


def parse_time(value) -> datetime.datetime | None:
    """库里的时间统一成带时区的 UTC；解不开或为空返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def minutes_since(value, now: datetime.datetime) -> float | None:
    parsed = parse_time(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds() / 60.0)


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def require_tables(conn: sqlite3.Connection, names, where: str) -> None:
    present = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = [name for name in names if name not in present]
    if missing:
        raise AuditError(
            f"SCHEMA_MISMATCH：{where} 缺表 {', '.join(missing)}，无法判断等待"
        )


# ---------------------------------------------------------------- 五类判定


def ticket_rows(conn: sqlite3.Connection, state: str) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            "SELECT id, state, title FROM tickets WHERE state=?", (state,)
        ).fetchall()
    except sqlite3.Error as exc:
        raise AuditError(f"SCHEMA_MISMATCH：读不了 tickets 表（{exc}）") from exc


def latest_submission(conn: sqlite3.Connection, ticket_id: str):
    """该工单全部尝试里 `created_at` 最大的一条提交。

    返工之后 `current_attempt` 指向新尝试，旧提交仍在表里，所以按全部尝试取最新，
    不只看当前尝试（PRD-010 §4.4 的实现口径同一条）。
    """
    try:
        rows = conn.execute(
            "SELECT s.id, s.created_at FROM submissions s"
            " JOIN attempts a ON a.id = s.attempt_id"
            " WHERE a.ticket_id=?",
            (ticket_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise AuditError(f"SCHEMA_MISMATCH：读不了 submissions / attempts（{exc}）") from exc
    best = None
    best_at = None
    for row in rows:
        at = parse_time(row["created_at"])
        if at is None:
            continue
        if best_at is None or at > best_at:
            best, best_at = row, at
    return best


def has_review(conn: sqlite3.Connection, submission_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM reviews WHERE submission_id=? LIMIT 1", (submission_id,)
    ).fetchone()
    return row is not None


def scan_waiting_review(conn, now, hits, ignored) -> None:
    """S3：待验收超过 4 小时且没有 review 行。"""
    for row in ticket_rows(conn, "awaiting_review"):
        submission = latest_submission(conn, row["id"])
        if submission is None:
            ignored.append({
                "kind": WAITING_REVIEW, "ticket_id": row["id"],
                "reason": "no_submission", "detail": "状态是 awaiting_review，但表里没有提交",
            })
            continue
        if has_review(conn, submission["id"]):
            ignored.append({
                "kind": WAITING_REVIEW, "ticket_id": row["id"],
                "reason": "already_reviewed", "detail": "已有 review 行，状态机慢一步而已",
            })
            continue
        minutes = minutes_since(submission["created_at"], now)
        if minutes is None:
            ignored.append({
                "kind": WAITING_REVIEW, "ticket_id": row["id"],
                "reason": "no_created_at", "detail": "提交没有可解析的 created_at",
            })
            continue
        if minutes <= AWAITING_REVIEW_HOURS * 60.0:
            continue
        hits.append({
            "kind": WAITING_REVIEW,
            "ticket_id": row["id"],
            "title": row["title"] or "",
            "detail": f"最新提交 {submission['id']} 至现在 {minutes / 60.0:.1f} 小时，无 review",
            "hours": round(minutes / 60.0, 1),
            "submission_id": submission["id"],
        })


def scan_awaiting_review_no_notice(conn, now, hits) -> None:
    """S3a：待验收、提交超过 20 分钟、且这张单一条回执记录都没有。

    计时起点是提交的 `created_at`，不是工单的 `created_at`——后者会把排队与派发的
    时间算进去，冷启动的单会假报。`notices` 判的是「这张单一行都没有」（`failed`
    也算走过回执），不是「最新提交之后没有」。
    """
    for row in ticket_rows(conn, "awaiting_review"):
        submission = latest_submission(conn, row["id"])
        if submission is None:
            # 没提交就没什么可等：这一档只报「交了、但没人接」，缺提交由
            # `waiting_review` 的 `no_submission` 记在 ignored 里。
            continue
        notice = conn.execute(
            "SELECT 1 FROM notices WHERE ticket_id=? LIMIT 1", (row["id"],)
        ).fetchone()
        if notice is not None:
            continue
        minutes = minutes_since(submission["created_at"], now)
        if minutes is None or minutes <= AWAITING_REVIEW_NO_NOTICE_MINUTES:
            continue
        hits.append({
            "kind": AWAITING_REVIEW_NO_NOTICE,
            "ticket_id": row["id"],
            "title": row["title"] or "",
            "detail": (
                f"最新提交 {submission['id']} 已 {minutes:.1f} 分钟，"
                "notices 一行都没有：回执大概率没发出去"
            ),
            "minutes": round(minutes, 1),
            "submission_id": submission["id"],
        })


def scan_done(conn, now, hits) -> None:
    """done 但没回执 / done 但没合并：两个独立的计数，不合并成一条。"""
    for row in ticket_rows(conn, "done"):
        notice = conn.execute(
            "SELECT 1 FROM notices WHERE ticket_id=? LIMIT 1", (row["id"],)
        ).fetchone()
        if notice is None:
            hits.append({
                "kind": DONE_NO_NOTICE,
                "ticket_id": row["id"],
                "title": row["title"] or "",
                "detail": "notices 为空：回执发没发事后查不出来",
            })
        merge = conn.execute(
            "SELECT 1 FROM merges WHERE ticket_id=? LIMIT 1", (row["id"],)
        ).fetchone()
        if merge is None:
            hits.append({
                "kind": DONE_NOT_MERGED,
                "ticket_id": row["id"],
                "title": row["title"] or "",
                "detail": "merges 无行：已验收但库里没有合并记录",
            })


def session_activity(sessions_conn, columns) -> dict[str, sqlite3.Row]:
    wanted = ["id", "title", "last_activity_at"]
    for name in ("active_turn_started_at", "last_turn_ended_at"):
        if name in columns:
            wanted.append(name)
    try:
        rows = sessions_conn.execute(
            f"SELECT {', '.join(wanted)} FROM sessions"
        ).fetchall()
    except sqlite3.Error as exc:
        raise AuditError(f"SCHEMA_MISMATCH：读不了 sessions 表（{exc}）") from exc
    return {row["id"]: row for row in rows}


def turn_is_active(row) -> bool:
    """§4.2「会话在跑」：`active_turn_started_at` 非空，且 `last_turn_ended_at` 空或更早。

    正在跑这一轮的长命令不算卡住：它没有新消息，但它没停在等人。
    """
    keys = row.keys()
    if "active_turn_started_at" not in keys:
        return False
    started = parse_time(row["active_turn_started_at"])
    if started is None:
        return False
    ended = parse_time(row["last_turn_ended_at"]) if "last_turn_ended_at" in keys else None
    return ended is None or ended < started


def scan_running_idle(ticket_conn, sessions_conn, now, idle_minutes, hits, ignored) -> None:
    """running 尝试的执行会话超过 10 分钟没有活动（PRD-010 §4.3 S4 的阈值）。"""
    columns = table_columns(sessions_conn, "sessions")
    if "last_activity_at" not in columns:
        raise AuditError("SCHEMA_MISMATCH：会话库 sessions 表没有 last_activity_at 列")
    sessions = session_activity(sessions_conn, columns)
    try:
        attempts = ticket_conn.execute(
            "SELECT id, ticket_id, session_id FROM attempts WHERE state='running'"
        ).fetchall()
    except sqlite3.Error as exc:
        raise AuditError(f"SCHEMA_MISMATCH：读不了 attempts 表（{exc}）") from exc
    titles = {
        row["id"]: (row["title"] or "")
        for row in ticket_conn.execute("SELECT id, title FROM tickets").fetchall()
    }
    for attempt in attempts:
        session_id = attempt["session_id"]
        base = {
            "kind": RUNNING_IDLE,
            "ticket_id": attempt["ticket_id"],
            "attempt_id": attempt["id"],
            "session_id": session_id,
        }
        if not session_id:
            ignored.append({**base, "reason": "no_session",
                            "detail": "尝试还没有绑定执行会话"})
            continue
        row = sessions.get(session_id)
        if row is None:
            ignored.append({**base, "reason": "session_missing",
                            "detail": "会话库里查不到这条会话，无法看活动时间"})
            continue
        if turn_is_active(row):
            ignored.append({**base, "reason": "turn_active",
                            "detail": "会话正在跑这一轮，长命令不算久无活动"})
            continue
        minutes = minutes_since(row["last_activity_at"], now)
        if minutes is None:
            ignored.append({**base, "reason": "no_activity_time",
                            "detail": "last_activity_at 为空，不参与判定"})
            continue
        if minutes <= idle_minutes:
            continue
        hits.append({
            **base,
            "title": titles.get(attempt["ticket_id"], ""),
            "detail": f"尝试在跑，执行会话 {minutes:.1f} 分钟没有活动",
            "minutes": round(minutes, 1),
        })


def scan(ticket_conn, sessions_conn, now=None, idle_minutes: float = SESSION_IDLE_MINUTES) -> dict:
    """扫一遍，返回结构化结果（人读输出与 --json 都从这里出）。"""
    now = now_utc() if now is None else now
    for conn in (ticket_conn, sessions_conn):
        if conn.row_factory is not sqlite3.Row:
            # 调用方拿到的是普通连接（夹具、嵌入式使用）时补上，下面的 row["列名"] 才成立。
            conn.row_factory = sqlite3.Row
    require_tables(
        ticket_conn,
        ("tickets", "attempts", "submissions", "reviews", "notices", "merges"),
        "工单库",
    )
    require_tables(sessions_conn, ("sessions",), "会话库")

    hits: list[dict] = []
    ignored: list[dict] = []
    scan_waiting_review(ticket_conn, now, hits, ignored)
    scan_awaiting_review_no_notice(ticket_conn, now, hits)
    scan_done(ticket_conn, now, hits)
    scan_running_idle(ticket_conn, sessions_conn, now, idle_minutes, hits, ignored)

    order = {kind: index for index, kind in enumerate(KINDS)}
    hits.sort(key=lambda item: (order[item["kind"]], item.get("ticket_id") or ""))
    summary = {kind: 0 for kind in KINDS}
    for hit in hits:
        summary[hit["kind"]] += 1
    return {
        "generated_at": now.isoformat(),
        "idle_minutes": idle_minutes,
        "awaiting_review_hours": AWAITING_REVIEW_HOURS,
        "awaiting_review_no_notice_minutes": AWAITING_REVIEW_NO_NOTICE_MINUTES,
        "hits": hits,
        "ignored": ignored,
        "summary": summary,
    }


# ---------------------------------------------------------------- 输出


def human_line(item: dict) -> str:
    """一行一条：`<类别> <等多久> <工单号> <标题>（细节）`。"""
    if item["kind"] == WAITING_REVIEW:
        shown = f"{item['hours']:.1f}h"
    elif item["kind"] in (AWAITING_REVIEW_NO_NOTICE, RUNNING_IDLE):
        shown = f"{item['minutes']:.1f}min"
    else:
        shown = "-"
    fields = [item["kind"], shown, item["ticket_id"] or "", item["title"]]
    return " ".join(fields).rstrip() + f"（{item['detail']}）"


def summary_line(result: dict) -> str:
    """末行固定五类各几条：全零也打，静默才看不出问题。"""
    parts = [f"{KIND_LABELS[kind]} {result['summary'][kind]} 条" for kind in KINDS]
    return "合计：" + "；".join(parts)


def render_human(result: dict) -> str:
    lines = [human_line(item) for item in result["hits"]]
    lines.append(summary_line(result))
    return "\n".join(lines)


# ---------------------------------------------------------------- 入口


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="wait_audit.py",
        description="扫一遍工单库与会话库：谁在等谁而没人知道（只读）。",
        epilog="退出码：0 五类都为空；1 有命中；2 参数或库有问题。",
    )
    parser.add_argument(
        "--db", default=None, metavar="PATH",
        help=f"工单中心库；默认 {default_ticket_db()}",
    )
    parser.add_argument(
        "--sessions-db", default=None, metavar="PATH",
        help="Cindy 会话库；默认取 Cindy 目录下 mtime 最新的 cindy-*.db",
    )
    parser.add_argument(
        "--minutes", type=float, default=SESSION_IDLE_MINUTES, metavar="N",
        help=f"running 尝试的执行会话多久没活动算久无活动"
             f"（默认 {SESSION_IDLE_MINUTES:g}，出处 PRD-010 §4.3 S4）",
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
    except SystemExit as exc:  # --help 走这里，退出码 0；参数错是 2
        return int(exc.code or 0)

    if args.minutes <= 0:
        print("错误：--minutes 必须是正数", file=sys.stderr)
        return 2

    sessions_path = Path(args.sessions_db) if args.sessions_db else default_sessions_db()
    if sessions_path is None:
        print(f"DB_NOT_FOUND：找不到 Cindy 会话库（在 {cindy_home()} 下没有 cindy-*.db）",
              file=sys.stderr)
        return 2
    ticket_path = Path(args.db) if args.db else default_ticket_db()

    ticket_conn = sessions_conn = None
    try:
        ticket_conn = read_only(ticket_path)
        sessions_conn = read_only(sessions_path)
        result = scan(ticket_conn, sessions_conn, idle_minutes=args.minutes)
    except AuditError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"SCHEMA_MISMATCH：读库失败（{exc}）", file=sys.stderr)
        return 2
    finally:
        for conn in (ticket_conn, sessions_conn):
            if conn is not None:
                conn.close()

    result["db"] = str(ticket_path)
    result["sessions_db"] = str(sessions_path)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_human(result))
    return 1 if result["hits"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
