#!/usr/bin/env python
"""给空判据单记一笔「已经点名报过」：AW-140 的写入口。

为什么要有它：`tools/next_action.py` 的空判据档（`fix_pool`）只报不改——它拿到的库是
`mode=ro`，一行都不写。而「报过几次」必须落库，否则下一轮读不出任何区别，同一张单会被
一字不变地重报一遍，AW-138 补的那一档就成了永久唤醒源。

落库这件事本身是 `task_center` 的 `record_pool_report` 写操作（一条事件，不改状态、
不写判据、不取消单）。本脚本只做一件事：把那一次调用的 payload 拼好，省得每个巡检
会话自己拼 JSON、自己起 `python -m task_center`。

用法（在工作区根目录，装出来的布局里 `tools/` 就在那儿）：

    python tools/pool_report.py --project ai-workspace --ticket AW-990 --request-id pool-AW-990-1
    python tools/pool_report.py --project ai-workspace --ticket AW-990 --session-id <会话 id> --note "第 1 轮点名"
    python tools/pool_report.py --project ai-workspace --ticket AW-990 --request-id ... --json   # 机器读

**一次一张单。** 一轮里报了三张就调三次：`request_id` 是幂等的单位，一单一个编号
重试才不会把两张单记成一次。同一轮重试请重用同一个 `--request-id`（回执原样返回，
不会多记一轮）。

退出码：`0` 记上了（或被同一请求号重放）；`1` 被拒（判据不为空、单不在了、状态不对）；
`2` 参数写错或库读不开——**「不知道」不许长得像「记上了」**。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
for entry in (TOOLS, ROOT):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import notice_payload  # noqa: E402  (它已经有一份「主检出那份库」的找法，不在这里再写一个)


def build_payload(args) -> dict:
    # `project_id` 不许省：`task_center` 按号取单时用它核对归属（`PROJECT_MISMATCH`），
    # 不是装饰。让脚本从库里「猜」一个项目，等于把归属这件事挪到猜出来的地方。
    payload = {"project_id": args.project, "ticket_id": args.ticket}
    if args.session_id:
        payload["session_id"] = args.session_id
    if args.note:
        payload["note"] = args.note
    return payload


def main(argv=None) -> int:
    try:  # Windows 控制台默认 GBK，中文一打印就 UnicodeEncodeError
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(
        description="给一张空判据单记一笔「已经点名报过」（默认写库）。")
    parser.add_argument("--ticket", required=True, help="工单号，例如 AW-990")
    parser.add_argument("--request-id", default=None,
                        help="本次调用的唯一编号；同一轮重试请重用同一个编号")
    parser.add_argument("--project", required=True, help="项目 id，例如 ai-workspace")
    parser.add_argument("--session-id", default=None, help="谁报的（自报，可省）")
    parser.add_argument("--note", default=None, help="一句话备注（可省）")
    parser.add_argument("--db", default=None,
                        help="工单库（默认主检出那份；在 .worktrees/<工单号>/ 里跑也能找到）")
    parser.add_argument("--json", action="store_true", help="输出结构化回执")
    args = parser.parse_args(argv)

    if not args.request_id:
        print("INVALID_INPUT: 缺 --request-id：没有它就没法幂等，重试会把同一轮记成两轮",
              file=sys.stderr)
        return 2

    db = Path(args.db).absolute() if args.db else notice_payload.default_ticket_db()
    from task_center import Center  # 放在这里：参数写错时不必加载写入侧

    try:
        response = Center(str(db)).execute("record_pool_report", build_payload(args),
                                           args.request_id)
    except (OSError, ValueError) as exc:
        print(f"DB_UNREADABLE: {db}（{exc}）", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(response, ensure_ascii=False, indent=2))
    elif response.get("ok"):
        result = response["result"]
        print(f"已记：{result['ticket_id']} 第 {result['reported_rounds']} 轮点名"
              f"（{result['state']}，{result['reported_at']}）")
    else:
        error = response.get("error") or {}
        print(f"{error.get('code', 'REJECTED')}: {error.get('message', '')}", file=sys.stderr)
    # 约定好的三档：记上了 / 被拒 / 查不出（库或参数）。被拒时回执已经打到 stderr。
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
