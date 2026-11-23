#!/usr/bin/env python
"""接替责任人：派单器 claim 之后，把 owner 交给一条**活着的**常驻会话（AW-122）。

为什么要有这个脚本：无人值守这条链上，「回执的收件人」是 `tickets.owner_session_id`
（现在负责收尾的那条会话），而 `claim_ticket` 写下的 owner 就是派发它的那条会话——
调度建的那种会话在自己这一轮结束时会被宿主归档（2026-11-22 实测：AW-106 的协调会话
`404129f5-…` 在 `last_turn_ended_at` 之后 22 ms 变成 `status=archived`）。于是
`record_notice` 报 `NOT_COORDINATOR`、`answer_ticket` 报 `NOT_COORDINATOR`、
`merge_ticket` 报 `NO_NOTICE`，一条已经验收完的单就锁死了。

接替本身 `take_over_ticket` 早就有了，缺的是「什么时候该接、接给谁、拿什么证明」：

    python tools/handover_owner.py --ticket AW-122 --to <常驻会话 id> --reason "<一句>"

它先只读核四件事，四条都过才真的写库：

1. **当前 owner 已经不在。** 宿主会话库里它是 `archived` 或查不到（`TAKEOVER_NEEDED`）。
   owner 还活着（`OWNER_ACTIVE`）就拒绝（`OWNER_STILL_ACTIVE`）：那时候要做的是把消息
   发回它自己，不是换人。今天的一次现场就是这样：一个会话在另一条协调会话还活着的时候
   把 owner 接到了自己身上，理由写的却是「执行会话提交后没记回执」——而那条回执在库里。
2. **目标会话活着。** 宿主里不是 `archived`、也不是查不到（`TARGET_NOT_ALIVE`）。
   接到一条死会话上等于把这次的死锁复刻一遍。
3. **这张单还有收尾动作可做。** `done` 且已经有合并记录或已有回执时拒绝
   （`NOTHING_TO_TAKE_OVER`）——合并根本不认会话（`merge_ticket` 只看 `done` 与
   `notices` 非空），换人不改变任何结果，只会多出两个都以为自己该写合并记录的会话。
4. **新 owner 不是这条尝试的执行会话。** 那等于自派自做（`TAKEOVER_BY_EXECUTOR`），
   `SELF_DISPATCH` 会从后门失效。

退出码沿用 `coordinator_state.py` 的三档：0 = 接替成功（`HANDOVER_DONE`）；
1 = 拒绝（`REFUSED`，并点名是哪一条）；2 = 查不出（`UNKNOWN`：工单库或宿主会话库
读不了、owner 状态无法判定——**「不知道」不许长得像「可以接」**）。

只读那一步与 `coordinator_state.py` 用的是同一个 `read_only`（`mode=ro` +
`PRAGMA query_only`），写库那一步经 `task_center` 的 `take_over_ticket`，事件照旧留痕。
`--dry-run` 只算不写，用来先看一眼结论。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import coordinator_state  # noqa: E402
import notice_payload  # noqa: E402
from coordinator_state import NEEDS_TAKEOVER, read_only  # noqa: E402

HANDOVER_DONE = "HANDOVER_DONE"
REFUSED = "REFUSED"
UNKNOWN = "UNKNOWN"


class Refused(RuntimeError):
    """拒绝接替：带一个可判的码与一句人话，由 main 打到 stdout 并以退出码 1 结束。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Undecidable(RuntimeError):
    """查不出结论：库读不了、宿主会话库找不到。退出码 2，绝不当成「可以接」。"""


def ticket_row(conn, ticket_id):
    try:
        row = conn.execute(
            "SELECT id, project_id, state, owner_session_id, current_attempt FROM tickets WHERE id=?",
            (ticket_id,)).fetchone()
    except Exception as exc:  # sqlite3.Error，但不为此引一个只用来接异常的 import
        raise Undecidable(f"读不了 tickets 表（{exc}）") from exc
    if row is None:
        raise Undecidable(f"工单不存在：{ticket_id}（库：{conn.execute('PRAGMA database_list').fetchone()[2]}）")
    return dict(row)


def wrap_up_left(conn, ticket_id):
    """这张单还有没有「非 owner 不可」的收尾动作：合并记录与回执各查一次。

    两条都查，因为它们是两件事：有合并记录 = 已经合完，没人需要再做任何事；
    有回执但没合并 = 收尾只剩合并，而合并只认 `done` + `notices` 非空，不认会话。
    """
    merged = conn.execute("SELECT 1 FROM merges WHERE ticket_id=? LIMIT 1", (ticket_id,)).fetchone()
    noticed = conn.execute("SELECT 1 FROM notices WHERE ticket_id=? LIMIT 1", (ticket_id,)).fetchone()
    return bool(merged), bool(noticed)


def session_status(sessions_conn, session_id):
    """宿主里这条会话的状态：`archived` / `active` / `missing` / None（查不出）。"""
    if sessions_conn is None:
        return None
    row = sessions_conn.execute("SELECT status FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        return "missing"
    return row["status"] or "unknown"


def decide(db, sessions_db, ticket_id, target, reason):
    """算出「该不该接、接给谁」，不做任何写入。返回一份可打印的事实。"""
    with _open(db) as conn:
        ticket = ticket_row(conn, ticket_id)
        merged, noticed = wrap_up_left(conn, ticket_id)
        attempt = None
        if ticket["current_attempt"]:
            row = conn.execute("SELECT id, session_id, coordinator_session_id FROM attempts WHERE id=?",
                               (ticket["current_attempt"],)).fetchone()
            attempt = dict(row) if row is not None else None
    if not reason or not reason.strip():
        raise Refused("REASON_REQUIRED", "接替必须写一句理由：events 里那一行是事后唯一能查到的「为什么换人」")
    if ticket["state"] == "cancelled":
        raise Refused("CANCELLED", f"{ticket_id} 已取消，没有收尾工作可以接手")
    if ticket["owner_session_id"] == target:
        raise Refused("ALREADY_OWNER", f"{target} 已经是 {ticket_id} 的 owner，没有东西可接替")
    if attempt is not None and attempt["session_id"] == target:
        raise Refused("TAKEOVER_BY_EXECUTOR",
                      f"{target} 是这条尝试的执行会话；执行会话不能给自己接替责任人（那等于自派自做）")
    if ticket["state"] == "done" and (merged or noticed):
        raise Refused(
            "NOTHING_TO_TAKE_OVER",
            f"{ticket_id} 已经 done，且{'合并记录已写' if merged else '回执已记'}——"
            "收尾只剩合并这一步，而合并只看 done 与 notices 非空、不认会话；"
            "换 owner 不会让任何动作变得可做，只会多出一个也以为自己该写合并记录的会话。"
            "合并走 python tools/auto_merge.py --ticket " + ticket_id)
    if sessions_db is None:
        raise Undecidable("找不到宿主会话库：核不出当前 owner 在不在、目标会话活不活。"
                          "用 --sessions-db 指定一个（默认取 %APPDATA%/Cindy 下最新的 cindy-*.db）")
    with _open(sessions_db) as sessions_conn:
        owner_status = session_status(sessions_conn, ticket["owner_session_id"])
        target_status = session_status(sessions_conn, target)
    if owner_status is None or target_status is None:
        raise Undecidable("宿主会话库读不出 sessions 表：不知道 owner 在不在，就不许接。")
    if owner_status not in NEEDS_TAKEOVER:
        raise Refused(
            "OWNER_STILL_ACTIVE",
            f"当前 owner {ticket['owner_session_id']} 在宿主里还是 `{owner_status}`，它没有不在："
            "要它收尾就把消息发回它自己（send_to_session 带 target_session_id），"
            "或者等它这一轮结束再看。接替是兜底通道，不是换人的常规入口——"
            "今天那次「两个会话都以为自己该写合并记录」就是这么来的。")
    if target_status in NEEDS_TAKEOVER:
        raise Refused(
            "TARGET_NOT_ALIVE",
            f"目标会话 {target} 在宿主里是 `{target_status}`：接到一条已经不在的会话上，"
            "等于把这次死锁原样复刻一遍。换一条还在的常驻会话，或先把它叫起来再核一次。")
    if target_status == "unknown":
        raise Refused("TARGET_NOT_ALIVE",
                      f"目标会话 {target} 的宿主状态是 `unknown`：判不出它在不在，不接")
    return {"ticket": ticket, "attempt": attempt, "owner_status": owner_status,
            "target_status": target_status, "merged": merged, "noticed": noticed,
            "target": target, "reason": reason}


class _open:
    """把 `read_only` 包成 with：用完就关，别把句柄留在进程里。"""

    def __init__(self, path):
        self.conn = None
        try:
            self.conn = read_only(path)
        except coordinator_state.StateError as exc:
            raise Undecidable(str(exc)) from exc

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc_info):
        self.conn.close()
        return False


def perform(db, plan):
    """真的写一行 owner：经 `task_center` 的 `take_over_ticket`，不自己拼 SQL。"""
    from task_center import Center

    result = Center(db).execute("take_over_ticket", {
        "project_id": plan["ticket"]["project_id"],
        "ticket_id": plan["ticket"]["id"],
        "new_owner_session_id": plan["target"],
        "reason": plan["reason"],
    }, f"handover-{uuid4()}")
    if not result.get("ok"):
        raise Refused(result.get("error", {}).get("code", "REFUSED"),
                      result.get("error", {}).get("message", "take_over_ticket 被拒"))
    return result["result"]


def render(plan, receipt, dry_run):
    ticket = plan["ticket"]
    lines = [
        f"ticket: {ticket['id']}",
        f"owner_before: {ticket['owner_session_id']}（宿主 status={plan['owner_status']}）",
        f"owner_after: {plan['target']}（宿主 status={plan['target_status']}）",
        f"origin: {plan['attempt']['coordinator_session_id'] if plan['attempt'] else '(none)'}"
        "（谁派的这条尝试：历史事实，接替不改它）",
        f"verdict: {HANDOVER_DONE}{'（--dry-run：没有写库）' if dry_run else ''}",
    ]
    if receipt:
        lines.append(f"event_reason: {receipt.get('reason')}")
    lines += [
        "next: python tools/coordinator_state.py --ticket "
        f"{ticket['id']}   # 期望 OWNER_ACTIVE",
        "next: python tools/notice_payload.py --ticket "
        f"{ticket['id']}   # record_notice 的 session_id 与 target_session_id 都是新 owner",
    ]
    return "\n".join(lines)


def main(argv=None):
    try:  # Windows 控制台默认 GBK，中文说明重定向就变乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="把工单的责任人接替到一条活着的常驻会话（默认写库）。")
    parser.add_argument("--ticket", required=True, help="工单号，例如 AW-122")
    parser.add_argument("--to", required=True, help="新 owner 的会话标识（原值，不拼不缩写）")
    parser.add_argument("--reason", required=True, help="为什么接替：写进 events 的那一句")
    parser.add_argument("--db", default=None,
                        help="工单库（默认主检出那份；在 .worktrees/<工单号>/ 里跑也能找到）")
    parser.add_argument("--sessions-db", default=None,
                        help="宿主会话库（默认按 mtime 取最新的 cindy-*.db）")
    parser.add_argument("--dry-run", action="store_true", help="只算不写：看结论，不动库")
    args = parser.parse_args(argv)

    # 库路径先落到绝对路径：`read_only` 用 `Path.as_uri()` 开只读连接，相对路径会抛 ValueError。
    db = Path(args.db).absolute() if args.db else notice_payload.default_ticket_db()
    sessions_db = (Path(args.sessions_db).absolute() if args.sessions_db
                   else coordinator_state.default_sessions_db())
    try:
        plan = decide(db, sessions_db, args.ticket, args.to, args.reason)
        receipt = None if args.dry_run else perform(db, plan)
    except Refused as exc:
        print(f"{REFUSED}: {exc.code}")
        print(str(exc))
        return 1
    except Undecidable as exc:
        print(f"{UNKNOWN}: {exc}", file=sys.stderr)
        return 2
    print(render(plan, receipt, args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
