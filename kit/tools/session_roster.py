#!/usr/bin/env python
"""会话名册：这条会话绑在哪张工单、处在什么状态；没绑就当场说不是执行者。

为什么要有这个脚本：幽灵会话的起因是「回执发错了地方」，但另一头同样常见——
一个会话被唤醒之后以为自己该推进流程，其实它没有绑在任何活跃尝试上。
这件事以前只能靠读库或靠人记，现在一条命令给结论。

    python tools/session_roster.py --session <session-id>
    python tools/session_roster.py --session <id> --db <中心库>

只读：`mode=ro` 打开现有库，`PRAGMA query_only`，版本不匹配照样能查
（名册只读 attempts / tickets 两列，不依赖某次 schema 升级）。**不建库、不写行，
也不修任何会话**：误建的新会话要停掉仍由人来做，本脚本只告诉你它绑没绑。

判据（与 PRD-009 的交接面一致）：

- **绑上了**：这张会话是一条尝试的 `session_id`，且工单与尝试都还在
  `dispatching` / `running` / `awaiting_review` / `blocked` 之内 → 退出码 0，
  打印工单号、项目、状态与尝试编号。
- **没绑**：没有任何尝试、或只绑在已终结的尝试上（`accepted` / `rejected` /
  `superseded`）→ 打印 `NOT_BOUND` 与「你不是任何工单的执行者，不要推进流程」，
  退出码 1。历史尝试照样列出来，方便看它曾经做过什么。
- 顺带列出「这张会话作为协调会话派过哪些单」：它不是执行者，但也不是路人。

退出码：0 绑在活跃尝试上；1 没绑；2 参数或库有问题（原因写 stderr）。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / ".data" / "task-center.sqlite3"

# 活跃 = 这张单还在跑；attempts 与 tickets 两边的状态名不完全一样
# （tickets 有 awaiting_review，attempts 是 submitted），所以两边都要判。
ACTIVE_TICKET_STATES = ("dispatching", "running", "awaiting_review", "blocked")
ACTIVE_ATTEMPT_STATES = ("dispatching", "running", "submitted", "blocked")

NOT_BOUND = "NOT_BOUND"


class RosterError(RuntimeError):
    """查不出结论时的唯一异常：带一句人能读的原因，由 main 打到 stderr。"""


def read_only(path):
    path = Path(path)
    if not path.is_file():
        raise RosterError(f"中心库不存在：{path}")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def executor_rows(conn, session_id):
    return conn.execute(
        "SELECT a.id AS attempt_id, a.state AS attempt_state, a.created_at,"
        " t.id AS ticket_id, t.project_id, t.title, t.state AS ticket_state, t.revision,"
        " a.coordinator_session_id"
        " FROM attempts a JOIN tickets t ON t.id=a.ticket_id"
        " WHERE a.session_id=? ORDER BY a.created_at, a.id", (session_id,)).fetchall()


def coordinator_rows(conn, session_id):
    return conn.execute(
        "SELECT DISTINCT a.ticket_id, t.project_id, t.state AS ticket_state"
        " FROM attempts a JOIN tickets t ON t.id=a.ticket_id"
        " WHERE a.coordinator_session_id=? ORDER BY a.ticket_id", (session_id,)).fetchall()


def is_active(row):
    return row["ticket_state"] in ACTIVE_TICKET_STATES and row["attempt_state"] in ACTIVE_ATTEMPT_STATES


def describe(row):
    return (f"  工单 {row['ticket_id']}（项目 {row['project_id']}，{row['ticket_state']}，"
            f"revision {row['revision']}）尝试 {row['attempt_id']}（{row['attempt_state']}）{row['title']}")


def main(argv=None):
    try:  # Windows 控制台默认 GBK，中文输出重定向就变乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="查一条会话绑在哪些工单上（只读）。")
    parser.add_argument("--session", required=True, help="要查的会话标识")
    parser.add_argument("--db", default=None, help=f"中心库路径（默认 {DEFAULT_DB}）")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else DEFAULT_DB
    session_id = args.session.strip()
    if not session_id:
        print("错误：--session 不能是空字符串", file=sys.stderr)
        return 2

    try:
        conn = read_only(db_path)
    except (RosterError, sqlite3.Error) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    try:
        rows = executor_rows(conn, session_id)
        coordinating = coordinator_rows(conn, session_id)
    except sqlite3.Error as exc:
        print(f"错误：查不了名册（{exc}）；这个文件不是工单中心的库？", file=sys.stderr)
        return 2
    finally:
        conn.close()

    if coordinating:
        print(f"这张会话作为协调会话派过 {len(coordinating)} 张单：")
        for row in coordinating:
            print(f"  工单 {row['ticket_id']}（项目 {row['project_id']}，{row['ticket_state']}）")

    active = [row for row in rows if is_active(row)]
    if active:
        print(f"这张会话绑在 {len(active)} 条活跃尝试上：")
        for row in active:
            print(describe(row))
        historical = [row for row in rows if not is_active(row)]
        if historical:
            print("同一条会话上的已终结尝试（不能据此推进）：")
            for row in historical:
                print(describe(row))
        return 0

    if rows:
        print(f"这张会话只绑在 {len(rows)} 条已终结的尝试上：")
        for row in rows:
            print(describe(row))
    else:
        print("这张会话没有绑在任何尝试上。")
    print(f"{NOT_BOUND}：你不是任何工单的执行者，不要推进流程。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
