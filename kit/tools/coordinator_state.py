#!/usr/bin/env python
"""责任人还在不在：一条命令给出「要不要接替」的结论。

为什么要有这个脚本：负责收尾的那条会话活在宿主里，库里只留一个字符串（`tickets.owner_session_id`；
它 claim 时等于 `attempts.coordinator_session_id`，之后可以被 `take_over_ticket` 换掉）。
调度建的那种会话在自己那一轮结束时会被宿主归档（2026-11-22 实测：AW-106 的协调会话
`404129f5-…` 的 `updated_at` 比 `last_turn_ended_at` 晚 22 ms，`sessions.status='archived'`；
同一天另两条调度会话结束本轮后仍是 `active`，所以「能不能收信」不能靠猜）。归档之后一条
回执既送不到、也落不了库（`record_notice` 只许协调会话调），一张已验收的单就卡在
`NO_NOTICE` 上。以前判这件事要打开宿主会话库手查 `sessions.status`。

    python tools/coordinator_state.py --ticket AW-106
    python tools/coordinator_state.py --ticket AW-106 --db <工单库> --sessions-db <cindy 库>

只读：工单库与宿主会话库都用 `mode=ro` + `PRAGMA query_only` 打开，一个字节都不写。

判据与退出码：

- `OWNER_ACTIVE`：当前 owner 在宿主里在（`status` 不是 `archived`）→ 退出 0。
- `TAKEOVER_NEEDED`：`status='archived'`，或宿主里查不到这条会话（`missing`）→ 退出 1，
  并打印接替要用的命令形状。
- `NO_OWNER` / `UNKNOWN`：这张单还没有 owner（没被领过）；会话库找不到、
  打不开或没有 `sessions` 表 → 退出 2。**「不知道」不许长得像「正常」**：这两种情况
  都不该被当成「协调会话还在」，也不该被当成「它已经归档」。

宿主会话库的默认位置与 `tools/idle_watch.py` 同一口径：`CINDY_HOME` 或
`%APPDATA%/Cindy` 下 mtime 最新的 `cindy-*.db`（排除备份与 WAL/SHM 旁文件）。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / ".data" / "task-center.sqlite3"

TAKEOVER_NEEDED = "TAKEOVER_NEEDED"
OWNER_ACTIVE = "OWNER_ACTIVE"
NO_OWNER = "NO_OWNER"
UNKNOWN = "UNKNOWN"

# 归档态与「宿主里已经没有这条会话」都要接替：两种情况下都没人能给这张单收口。
NEEDS_TAKEOVER = ("archived", "missing")


class StateError(RuntimeError):
    """查不出结论时的唯一异常：带一句人能读的原因，由 main 打到 stderr。"""


def read_only(path):
    # `as_uri()` 不接相对路径：给了相对的 --db 会抛 ValueError 而不是一句人话。
    path = Path(path).absolute()
    if not path.is_file():
        raise StateError(f"库不存在：{path}")
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.Error as exc:
        raise StateError(f"打不开库 {path}（{exc}）") from exc
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
    """默认会话库：`cindy-*.db` 里 mtime 最新的那个（排除备份与 WAL/SHM）。"""
    home = Path(home) if home else cindy_home()
    if not home.is_dir():
        return None
    candidates = []
    for path in home.glob("cindy-*.db"):
        name = path.name.lower()
        if ".bak" in name or name.endswith("-shm") or name.endswith("-wal"):
            continue
        if path.is_file():
            candidates.append(path)
    return max(candidates, key=lambda item: item.stat().st_mtime) if candidates else None


def owner_and_origin(conn, ticket_id):
    """这张单的 owner（现在谁负责）与 origin（谁派的当前这一代）。

    只读 tickets / attempts 几列，不依赖某次 schema 升级。`owner_session_id` 是 schema 15 才有的列
    （14 → 15 加的；14 那一版已经有 `requester_session_id` 与 `notices.message_id`），读不到就说明库还停在
    14 或更早——那时没有 owner 这一列，这里退回用 origin 顶上：claim 那一刻两者相等，能对上的只是
    「当时是谁派的」，不等于「现在还活着、还能收信」。
    """
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tickets)")}
        fields = "id, state, current_attempt" + (", owner_session_id" if "owner_session_id" in columns else "")
        ticket = conn.execute(f"SELECT {fields} FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    except sqlite3.Error as exc:
        raise StateError(f"读不了 tickets 表（{exc}）") from exc
    if ticket is None:
        raise StateError(f"工单不存在：{ticket_id}")
    origin = None
    if ticket["current_attempt"]:
        row = conn.execute("SELECT coordinator_session_id FROM attempts WHERE id=?",
                           (ticket["current_attempt"],)).fetchone()
        origin = row["coordinator_session_id"] if row is not None else None
    owner = ticket["owner_session_id"] if "owner_session_id" in columns else origin
    return ticket, owner, origin


def iso(ms):
    if not ms:
        return "none"
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def host_row(conn, session_id):
    try:
        return conn.execute("SELECT id, status, source, created_at, updated_at, last_turn_ended_at"
                            " FROM sessions WHERE id=?", (session_id,)).fetchone()
    except sqlite3.Error as exc:
        raise StateError(f"读不了宿主会话库的 sessions 表（{exc}）") from exc


def lookup(db, sessions_db, ticket_id):
    """把「这张单现在的责任人怎么样」算成一份可打印的事实。"""
    with _guard(read_only(db)) as ticket_conn:
        ticket, owner, origin = owner_and_origin(ticket_conn, ticket_id)
    state = {"ticket_id": ticket["id"], "owner_session_id": owner, "origin_session_id": origin,
             "host_status": "unknown", "host_source": "none",
             "last_turn_ended_at": "none", "updated_at": "none", "verdict": NO_OWNER}
    if not owner:
        return state
    state["verdict"] = UNKNOWN
    if sessions_db is None or not Path(sessions_db).is_file():
        # 宿主会话库找不到：结论是「不知道」，不是「还活着」。调用方据此退出 2。
        return state
    with _guard(read_only(sessions_db)) as sessions_conn:
        row = host_row(sessions_conn, owner)
    if row is None:
        state["host_status"] = "missing"
        state["verdict"] = TAKEOVER_NEEDED
        return state
    state["host_status"] = row["status"] or "unknown"
    state["host_source"] = row["source"] or "none"
    state["last_turn_ended_at"] = iso(row["last_turn_ended_at"])
    state["updated_at"] = iso(row["updated_at"])
    if state["host_status"] in NEEDS_TAKEOVER:
        state["verdict"] = TAKEOVER_NEEDED
    elif state["host_status"] == "unknown":
        state["verdict"] = UNKNOWN
    else:
        state["verdict"] = OWNER_ACTIVE
    return state


class _guard:
    """把只读连接包成 with 语句：用完就关，别把库句柄留在进程里。"""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc_info):
        self.conn.close()
        return False


EXIT_CODES = {OWNER_ACTIVE: 0, TAKEOVER_NEEDED: 1, NO_OWNER: 2, UNKNOWN: 2}


def render(state, db):
    lines = [
        f"ticket: {state['ticket_id']}",
        f"owner_session_id: {state['owner_session_id'] or '(none)'}",
        f"origin_session_id: {state['origin_session_id'] or '(none)'}",
        f"host_status: {state['host_status']}",
        f"host_source: {state['host_source']}",
        f"last_turn_ended_at: {state['last_turn_ended_at']}",
        f"updated_at: {state['updated_at']}",
        f"verdict: {state['verdict']}",
    ]
    if state["verdict"] == TAKEOVER_NEEDED:
        lines.append(
            "next: 由一个新的会话声明接替责任人（读 docs/protocol.md 的「协调会话归档之后」一节）：\n"
            f"  python -m task_center --db {Path(db).absolute()} take_over_ticket --request-id <编号> --input <payload>\n"
            "  payload 是 {project_id, ticket_id, new_owner_session_id, reason} 四个字段；"
            "接替之后 record_notice 与 merge_ticket 照原路走")
    return "\n".join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = argparse.ArgumentParser(description="责任人还在不在：要不要接替")
    parser.add_argument("--ticket", required=True, help="工单号，例如 AW-106")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="工单库（默认仓库里的 .data/task-center.sqlite3）")
    parser.add_argument("--sessions-db", default=None, help="宿主会话库（默认按 mtime 取最新的 cindy-*.db）")
    args = parser.parse_args(argv)
    sessions_db = Path(args.sessions_db) if args.sessions_db else default_sessions_db()
    try:
        state = lookup(args.db, sessions_db, args.ticket)
    except StateError as exc:
        print(f"{UNKNOWN}: {exc}", file=sys.stderr)
        return 2
    print(render(state, args.db))
    return EXIT_CODES[state["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
