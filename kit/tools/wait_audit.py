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

五类（前四类的判据与阈值来自 PRD-010，第五类来自一次实测，都不在这里发明新数）：

1. `waiting_review`：工单 `state='awaiting_review'`，最新一条提交在 `reviews` 里没有行，
   且提交到现在的时长超过 4 小时。出处：`PRD/PRD-010-项目进度面板.md` §4.3 S3。
   已经有 review 行、只是工单状态还没翻的，按「已有 review」不算——那是状态机慢一步，
   不是没人看。
2. `awaiting_receipt`：工单 `state='awaiting_review'`、最新一条提交在 `notices` 里
   没有任何一条 `sent`（写 `failed` 的不算走过回执），且提交到现在的时长超过 15 分钟。
   出处是 2026-11-22 的实测：AWK-101 的执行会话提交完就走了，一条回执也没发，
   这一类当时不存在，这个工具一声没响，是用户追问才发现的。4 小时那条（第 1 类）
   题的是「没人验收」，这一类题的是「回执根本没发出去」，两条链，各数各的。
   已经有 review 行的不报：验收行在那里就说明链条往前走过。
3. `done_no_notice`：工单 `state='done'` 但 `notices` 里一行都没有。闭环没走完，
   事后查不出回执到底发没发。
4. `done_not_merged`：工单 `state='done'` 但 `merges` 里没有行。已验收但没合。
   注意这一类比 PRD-010 §4.4 的「真待合并」宽：它不查分支，历史直做的单也会命中。
   这里报的是「库里没合并记录」这个事实，分「真待合并 / 历史直做」属于 §4.4 的分类，
   不在只读巡检里做。要那一层分类看面板或 `tools/auto_merge.py --list`。
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

退出码：0 = 五类都为空；1 = 有命中；2 = 参数或库有问题（原因写 stderr，缺库打印
`DB_NOT_FOUND`）。末行固定打印五类各几条，即使全零也一样——静默丢弃会让闸门看起来
永远绿或永远红，两种都不能用。
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
# 用户级指针：装好的工作区把中心库位置写在这里（与 work_gate / idle_watch 读的是同一份）。
CONFIG = Path.home() / ".agents" / "center.json"

# 阈值只在模块常量里写一次，旁边标出处；测试与方法文档都打到这里，不各写一份。
# 4 小时：PRD-010 §4.3 S3「待验收超时」。
AWAITING_REVIEW_HOURS = 4.0
# 10 分钟：PRD-010 §4.3 S4「会话悬着」。
SESSION_IDLE_MINUTES = 10.0
# 15 分钟：提交之后多久还没有 `sent` 回执就算「回执没发出去」。
# 这个数不在 PRD-010 里，出处是 2026-11-22 的实测：AWK-101 的执行会话提交完就结束了，
# 一条回执都没发，而当时只有「等在验收超过 4 小时」那一条，于是这个工具一声没响。
# 定在 15 分钟是为了让正常人写完回执不会被误报；真出现过误报就提到 30 分钟并写清理由。
AWAITING_RECEIPT_MINUTES = 15.0

# 五类的名字：人读输出、--json、末行合计都用这五个，不另起别名。
WAITING_REVIEW = "waiting_review"
AWAITING_RECEIPT = "awaiting_receipt"
DONE_NO_NOTICE = "done_no_notice"
DONE_NOT_MERGED = "done_not_merged"
RUNNING_IDLE = "running_idle"

KINDS = (WAITING_REVIEW, AWAITING_RECEIPT, DONE_NO_NOTICE, DONE_NOT_MERGED, RUNNING_IDLE)

# 末行合计的中文名：顺序与 KINDS 一致。
KIND_LABELS = {
    WAITING_REVIEW: "等在验收",
    AWAITING_RECEIPT: "等回执",
    DONE_NO_NOTICE: "done 没回执",
    DONE_NOT_MERGED: "done 没合并",
    RUNNING_IDLE: "running 久无活动",
}


def default_ticket_db() -> Path:
    """默认工单库：本检出的 `.data/task-center.sqlite3`。

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
    # 套件仓库里（`kit/` 下没有任何库）前两条都不成立，但用户级指针 `~/.agents/center.json`
    # 指的那份库是同一个唯一真相（与 work_gate 读的是同一份），所以照它读，不另建一个：
    # 巡检读错库报出来的「没有待办」比报不出来更坏。
    try:
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        configured = Path(str(config.get("db") or "")).expanduser()
        if configured.is_file():
            return configured
    except (OSError, ValueError):
        pass
    return local


class AuditError(RuntimeError):
    """查不出结论时的唯一异常：带一句人能读的原因，由 main 打到 stderr。"""


# ---------------------------------------------------------------- 库与时间


def read_only(path) -> sqlite3.Connection:
    """只读打开：`mode=ro` + `PRAGMA query_only`，与 idle_watch / session_roster 同一写法。"""
    path = Path(path)
    if not path.is_absolute():
        # `as_uri()` 只收绝对路径：相对路径会一路报 ValueError 而不是 DB_NOT_FOUND，
        # 一条用法错误看起来像工具崩了。先归一成绝对路径，剩下按原定判据走。
        path = path.resolve()
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


def has_sent_notice(conn: sqlite3.Connection, ticket_id: str) -> bool:
    """这张单有没有任何一条 `sent` 回执。写 `failed` 的不算：那一次没到任何人手里。"""
    try:
        row = conn.execute(
            "SELECT 1 FROM notices WHERE ticket_id=? AND status='sent' LIMIT 1", (ticket_id,)
        ).fetchone()
    except sqlite3.Error as exc:
        raise AuditError(f"SCHEMA_MISMATCH：读不了 notices 表（{exc}）") from exc
    return row is not None


def scan_awaiting_receipt(conn, now, hits, ignored) -> None:
    """提交后 15 分钟还没有一条 sent 回执（AWK-101 的现场）。

    与第 1 类（4 小时没人验收）是两条链：一条问「有没有人看」，这条问「回执发没发出去」。
    “等」从最新一条提交的 `created_at` 算起——那是回执应该发出的时刻。
    """
    for row in ticket_rows(conn, "awaiting_review"):
        submission = latest_submission(conn, row["id"])
        base = {"kind": AWAITING_RECEIPT, "ticket_id": row["id"]}
        if submission is None:
            ignored.append({**base, "reason": "no_submission",
                            "detail": "状态是 awaiting_review，但表里没有提交"})
            continue
        if has_review(conn, submission["id"]):
            ignored.append({**base, "reason": "already_reviewed",
                            "detail": "已有 review 行，链条已经往前走过了"})
            continue
        if has_sent_notice(conn, row["id"]):
            ignored.append({**base, "reason": "notice_sent",
                            "detail": "已有 sent 回执，不需要再报"})
            continue
        minutes = minutes_since(submission["created_at"], now)
        if minutes is None:
            ignored.append({**base, "reason": "no_created_at",
                            "detail": "提交没有可解析的 created_at"})
            continue
        if minutes <= AWAITING_RECEIPT_MINUTES:
            continue
        hits.append({
            **base,
            "title": row["title"] or "",
            "detail": (f"提交 {submission['id']} 已 {minutes:.1f} 分钟"
                       f"还没有 sent 回执（notices 里{sent_hint(conn, row['id'])}"),
            "minutes": round(minutes, 1),
            "submission_id": submission["id"],
        })


def sent_hint(conn: sqlite3.Connection, ticket_id: str) -> str:
    """报错里那一句「notices 里有什么」，让人不用再开库查一次。"""
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM notices WHERE ticket_id=? GROUP BY status",
            (ticket_id,),
        ).fetchall()
    except sqlite3.Error:
        return "读不到"
    if not rows:
        return "一条都没有）"
    return "只有 " + "、".join(f"{row['status']}×{row['n']}" for row in rows) + "）"


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
    scan_awaiting_receipt(ticket_conn, now, hits, ignored)
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
        "hits": hits,
        "ignored": ignored,
        "summary": summary,
    }


# ---------------------------------------------------------------- 输出


def human_line(item: dict) -> str:
    """一行一条：`<类别> <等多久> <工单号> <标题>（细节）`。"""
    if item["kind"] == WAITING_REVIEW:
        shown = f"{item['hours']:.1f}h"
    elif item["kind"] in (AWAITING_RECEIPT, RUNNING_IDLE):
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
