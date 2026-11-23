#!/usr/bin/env python
"""回执怎么发：按工单号打印 `send_to_session` 的完整入参（只读，不改库）。

为什么要有这个脚本：执行会话交回成果时要发一条回执给派它的协调会话，而这条路上
有两处纯靠记忆的地方——`send_to_session` 不带 `target_session_id` 时是**新建任务**
（回执变成一条没人认领的会话），以及「该发给谁」只写在 `attempts.coordinator_session_id`
里、不在派单消息里。两处记漏时当场都没有任何反应，只有事后查 `notices` 才发现。
所以把它做成一条命令：收件人从库里算，整份 args 打印出来，复制即可。

    python tools/receipt_payload.py --ticket AW-111
    python tools/receipt_payload.py --ticket AW-111 --attempt <attempt_id>

打印两段：

1. `send_to_session` —— 完整 args：`message`（工单号 / 状态 / 提交 sha）与
   `target_session_id`（这张单当前的 `tickets.owner_session_id`：现在负责收尾的那条会话；
   没有这一列的老库回退到 `attempts.coordinator_session_id`）。
2. `说明` —— 怎么调（工具列表里可能搜不到这个名字，要经 `cindy_helper` 的
   `call_tool` 调）、为什么不能省 `target_session_id`、以及落库那一步归谁。

与 `tools/notice_payload.py` 是同一件事的两头：这个脚本给**执行会话**发回执用，
那个给**当前 owner** 落 `record_notice` 用。两个都只读、都不发消息。

收件人（owner）缺失时非零退出并说明原因，**一个 args 都不打印**：打印出来的东西一定
会被复制走，没有 `target_session_id` 的那份会被当成「省略 target」发出去，宿主就
新建一条任务——那正是这条链上最难事后发现的一种错。

读的是**最新值**：只读连接的选路（库旁有没有非空 `-wal`）与 `notice_payload` 共用
一份，见那个脚本的 `read_only`；每次输出第二行都写明这次走了哪条路。

退出码：0 打印两段；2 参数、库或数据不满足（原因写 stderr）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from notice_payload import (
    PayloadError,
    default_ticket_db,
    pick_attempt,
    ticket_scope,
)

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DB = ROOT / ".data" / "task-center.sqlite3"


def target_of(ticket, attempt):
    """收件人：这张单**现在负责收尾的那条会话**（owner），取不到才回退 origin。

    为什么不是 origin（`attempts.coordinator_session_id`）：那个字段是「谁派的」，永不
    改写；回执该去的地方是「谁现在能把它收口」。`record_notice` 的授权与投递目标读的也
    是 owner，所以两边用同一个值，`NOT_TARGET` 才不会在 owner 与 origin 不相等时把人
    卡住（AW-122）。老库（schema 15 以前）没有这一列，那时 owner 与 origin 总是相等，
    回退不影响任何历史行为。
    """
    owner = ticket.get("owner_session_id")
    if owner:
        return owner
    coordinator = attempt.get("coordinator_session_id")
    if coordinator:
        return coordinator
    raise PayloadError(
        f"工单 {ticket['id']} 的尝试 {attempt['id']} 既没有 owner 也没有 coordinator_session_id："
        "这条尝试 claim 时没写收件人，回执没有确定的目标。不猜一个字符串，也不省掉 "
        "target_session_id 把回执发成一个新任务——先补记一次 claim（或 recover_ticket "
        "重派）再取参数。"
    )


def latest_submission(ticket):
    """最新一条提交；这张单还没提交过时是 None。

    提交按 `rowid` 排序（`notice_payload.read_ticket` 的取法），所以 `[-1]` 是最后
    写进去的那一条，与 `get_ticket` 的 `submissions[-1]` 是同一份。
    """
    submissions = ticket["submissions"]
    return submissions[-1] if submissions else None


def message_for(ticket):
    """回执正文只有三个事实：工单号、状态、提交 sha。

    不写 `attempt_id`、`revision`、行号这类几小时内就会失效的东西（正文进的是消息，
    不是记录）；也没有提交时不编一个 sha——照实说库里还没有提交。
    """
    parts = [ticket["id"], f"状态 {ticket['state']}"]
    last = latest_submission(ticket)
    if last is None:
        parts.append("库里还没有提交")
    elif last.get("head_sha"):
        parts.append(f"提交 {last['head_sha']}")
    else:
        # schema 8 以前的提交没有分支头，只有提交号：那也是能不能核对的唯一依据。
        parts.append(f"提交 {last['id']}")
    return " / ".join(parts)


def note_for(ticket, attempt):
    """说明那一段：怎么调、为什么 target 不能省、落库归谁。"""
    ticket_id = ticket["id"]
    owner = target_of(ticket, attempt)
    origin = attempt.get("coordinator_session_id") or "（未记）"
    return [
        f"1. 怎么调：send_to_session 在工具列表里可能搜不到这个名字（按名字搜会返回空或"
        f" UNKNOWN_TOOL），但它经 cindy_helper 的 call_tool 可以直接调——args 传 "
        f'{{"name": "send_to_session", "args": {{"message": <上面那段 message>, '
        f'"target_session_id": <上面那个 id>}}}}。',
        f"2. 为什么 target 不能省：send_to_session 不带 target_session_id 时它的语义是新建任务，"
        f"宿主回执的 wake_kind 就是 created，回执落到一条没人认领的新会话里。",
        f"3. 收件人是这张单**现在负责收尾的那条会话**（tickets.owner_session_id）：{owner}。"
        f"谁派的（origin）是 {origin}，那是历史，回执不再发给它（AW-122）。"
        f"不要写成执行会话自己，也不要手拼一个字符串。",
        f"4. 正文只写工单号、状态与提交 sha；wake_kind 不写进正文，它只出现在发送回执里。"
        f"落库那一步是当前 owner 的 record_notice，参数用 "
        f"python tools/notice_payload.py --ticket {ticket_id} 打印。",
        f"5. 发不出去时不要改记录：走 report_notice_failure，写清发给谁、失败在哪一步；"
        f"无人值守下这就是这条链的正常出口（回执落 failed，推进靠下一轮 next_action）。",
    ]


def block(name, payload):
    return [f"--- {name} ---", json.dumps(payload, ensure_ascii=False, indent=2)]


def render(ticket, attempt):
    """两段：可复制的 args，与一段人读的说明。"""
    send = {"message": message_for(ticket), "target_session_id": target_of(ticket, attempt)}
    return block("send_to_session", send) + ["--- 说明 ---"] + note_for(ticket, attempt)


def main(argv=None):
    try:  # Windows 控制台默认 GBK，中文说明重定向就变乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="从中心库生成回执的 send_to_session 入参（只读）。")
    parser.add_argument("--ticket", required=True, help="工单号，例如 AW-111")
    parser.add_argument("--attempt", default=None, help="尝试编号；默认取当前尝试")
    parser.add_argument("--project", default=None, help="项目标签；默认按工单号反查")
    parser.add_argument("--db", default=None,
                        help="中心库路径（默认主检出那份；在 .worktrees/<工单号>/ 里跑也能找到）")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else default_ticket_db()
    try:
        ticket, how = ticket_scope(db_path, args.ticket, args.project)
        attempt = pick_attempt(ticket, args.attempt)
        lines = render(ticket, attempt)
    except PayloadError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    print(f"# {ticket['id']}：复制下面那段 JSON，别改 target_session_id")
    print(f"# 读取方式：{how}")
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
