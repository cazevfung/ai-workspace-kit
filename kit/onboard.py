#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动层：把「装完之后怎么在宿主里开工」打印成一份能照着调的调用计划。

为什么要有这个脚本：`install.py` 只把文件铺到磁盘上，装完那一刻用户看到的是一堆
命令，而不是「下一步点哪里、传什么参数」。宿主能力（创建项目、开协调会话）不在
安装器里，也不该由安装器偷偷调用——它们是宿主操作，要显示在会话里、失败要能看见。
所以这里只产出计划：每一步用哪个工具、完整参数是什么、怎么反查、失败时停在哪。

    python onboard.py                  # 打印三步的人读计划
    python onboard.py --json           # 每步完整参数，供技能与插件复用
    python onboard.py --dry-run        # 预演：仍然只打印，不写任何文件、不调宿主
    python onboard.py --json --dry-run # 预演 + JSON

两条计划同源：

- `bootstrap`：list_projects → create_project → send_to_session → list_sessions →
  write_receipt（`local` 步），对应「装完 → 把工作区加成项目 → 开协调会话 → 发第一条
  指令 → 反查会话 → 本地写回执」。最后一步不是宿主工具：它由本地写文件完成，
  写 `<workspace>/.data/onboard-receipt.json`，四个字段见 `RECEIPT_FIELDS`。
- `dispatch_ticket`：get_ticket → claim_ticket → send_to_session → list_sessions，
  派发一张工单时用；其中 send_to_session / list_sessions 与 bootstrap 同一份参数。

退出码：0 打印成功；2 参数不合法（例如工作区目录不存在）。

依据：PRD-001 v0.1 的闭环形状与「归属不猜」；AGENTS.md 硬规则五（派发是新开一个
会话，执行者自己 bind_session）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows 控制台默认按 locale（中文机器上是 cp936）编码，管道另一端按 UTF-8 读就会
# 全部变成乱码。输出一律走 UTF-8，与 task_center 的命令行一致。
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
# 计划里出现的 cindy_helper 工具名必须都在清单里。这里显式列出来，改计划时会先
# 撞上 tools/test_onboard.py 与 tools/test_plugin_plan.py 的断言，而不是等用户装完
# 才发现某个名字在宿主里不存在。
CINDY_HELPER_TOOLS = ("list_projects", "create_project", "send_to_session", "list_sessions")
TASK_CENTER_TOOLS = ("get_ticket", "claim_ticket")
# `local` 步：不经过宿主，由会话在本地写文件。它故意不在上面两张清单里——
# 混进去就等于声称宿主有这个工具。
LOCAL_STEP_KIND = "local"
RECEIPT_RELATIVE = ".data/onboard-receipt.json"
RECEIPT_FIELDS = ("project_path", "target_session_id", "working_dir", "created_at")

FIRST_INSTRUCTION = (
    "你是这个 AI 工作区的协调会话。先读 AGENTS.md，再跑 `python tools/work_gate.py --list` "
    "确认没有在途工单；接着用 `python tools/run_tests.py` 与 `python tools/doc_gate.py --all` "
    "确认环境全绿。要开工单时按 `.agents/skills/ticket-dispatch/SKILL.md` 的顺序走："
    "create_ticket → claim_ticket → 新开执行会话 → bind_session。"
)

FAILURES = {
    "bootstrap": {
        "list_projects": "读不到项目清单就停下：不要凭名字猜路径，也不要为了继续而新建一个项目。",
        "create_project": "目录不存在或不是目录 → 先建目录；同名项目已存在 → 复用它的 project_path，不要重复登记；"
                          "返回 SOURCE_OUTSIDE_WORKDIR / 权限被拒 → 把工作区放到会话可写的目录再试。",
        "send_to_session": "返回 NOT_FOUND / ARCHIVED / DELETED → 不要重复新建项目；先 list_sessions 查实际状态。"
                           "返回 PROJECT_NOT_FOUND → 回到 create_project 的 project_path，不要传显示名。",
        "list_sessions": "列表里找不到 title 与 workdir 同时匹配的会话 → 会话没建出来，不要说已经开工；"
                         "重发一次 send_to_session，仍失败就停下问人。",
        "write_receipt": "没有反查到的 target_session_id → 不要拿猜的 id 凑一份回执；写不进去（目录只读、"
                         ".data 建不出来）→ 停下说明，不要说已经开工。回执只是一份声称，"
                         "它成立与否由 verify/verify_receipt.py 事后核。",
    },
    "dispatch_ticket": {
        "get_ticket": "工单不是 ready / rework → 不要重复领取；先看 state 说明它停在哪一步。",
        "claim_ticket": "DISPATCH_BUSY → 同项目已有在途单，先做完那张；SELF_DISPATCH → 换一个执行会话，"
                        "不要协调会话自己做；STALE_ATTEMPT → 重新 get_ticket 取当前 revision。",
        "send_to_session": "返回 NOT_FOUND / ARCHIVED / DELETED → 不要换一个字符串冒充执行者；先修会话再重派。"
                           "返回 created 但标题不是本单 → 回执发错了地方，停掉误建任务再带 target_session_id 重发。",
        "list_sessions": "找不到 title/workingDir 匹配的执行会话 → 派发未完成；不要自己 bind_session 顶替执行者。",
    },
}


def _send_step(order: int, workspace: str, title: str, message: str,
               agent_kind: str | None = None, model: str | None = None) -> dict:
    args = {
        "message": message,
        "title": title,
        "project_path": workspace,
    }
    if agent_kind:
        args["agent_kind"] = agent_kind
    if model:
        args["model"] = model
    return {
        "order": order,
        "tool": "send_to_session",
        "server": "cindy_helper",
        "call_via": "cindy_mcp_call_tool",
        "args": args,
        "mode": "create（不传 target_session_id 才是新建任务）",
        "check": "回执必须带 target_session_id 与 working_dir；把它原样交给下一步反查，不要改写或缩写。",
    }


def _list_sessions_step(order: int, workspace: str) -> dict:
    return {
        "order": order,
        "tool": "list_sessions",
        "server": "cindy_helper",
        "call_via": "cindy_mcp_call_tool",
        "args": {
            "workdir": workspace,
            "device": "local",
            "include_deleted": False,
            "limit": 100,
            "order": "desc",
        },
        "check": "看返回里有没有 title 与 workingDir 都对得上的那一条；对不上就是没建出来。",
    }


def _receipt_step(order: int, workspace: str) -> dict:
    """本地写回执：不在 cindy_helper 清单里，也不要冒充宿主工具。"""
    return {
        "order": order,
        "tool": "write_receipt",
        "server": "local",
        "kind": LOCAL_STEP_KIND,
        "call_via": "会话自己在本地写文件（不经宿主）",
        "args": {
            "path": (Path(workspace) / RECEIPT_RELATIVE).as_posix(),
            "target_session_id": "<第 3 步回执里的 target_session_id>",
            "working_dir": workspace,
            "fields": list(RECEIPT_FIELDS),
        },
        "check": "写完读一遍：四个字段都在、路径是绝对路径、target_session_id 与第 3 步回执一字不差；"
                 "核对不上不算写过。回执只是声称，证明不了宿主里项目真的开着。",
    }


def build_plans(workspace: str, *, project_name: str | None = None,
                title: str = "AI 工作区 · 协调会话",
                agent_kind: str | None = None, model: str | None = None,
                project_id: str = "ai-workspace", ticket_id: str = "<工单号>",
                expected_revision: int = 1) -> dict:
    """返回两条计划。纯函数：不读环境、不写文件、不调宿主。"""
    workspace = str(Path(workspace).expanduser().resolve())
    name = project_name or Path(workspace).name or "AI 工作区"
    bootstrap = {
        "purpose": "把已装好的工作区加成宿主项目，开一个协调会话，并把第一条指令发进去。",
        "steps": [
            {
                "order": 1,
                "tool": "list_projects",
                "server": "cindy_helper",
                "call_via": "cindy_mcp_call_tool",
                "args": {"include_hidden": True, "offset": 0, "limit": 100},
                "check": f"清单里已有 project_path == {workspace} 的项目 → 跳过 create_project，直接用它。",
            },
            {
                "order": 2,
                "tool": "create_project",
                "server": "cindy_helper",
                "call_via": "cindy_mcp_call_tool",
                "args": {"name": name, "working_dir": workspace},
                "check": "回执的 project_path 必须等于工作区绝对路径；把它传给 send_to_session。",
            },
            _send_step(3, workspace, title, FIRST_INSTRUCTION, agent_kind, model),
            _list_sessions_step(4, workspace),
            _receipt_step(5, workspace),
        ],
    }
    dispatch = {
        "purpose": "工单已经 claim 之后，新开一个执行会话并把工单号发过去；send_to_session 与 bootstrap 同一份参数。",
        "steps": [
            {
                "order": 1,
                "tool": "get_ticket",
                "server": "task_center",
                "call_via": "python -m task_center --db <中心库> get_ticket",
                "args": {"project_id": project_id, "ticket_id": ticket_id},
                "check": "state 必须是 ready / rework；不是就先看它停在哪一步。",
            },
            {
                "order": 2,
                "tool": "claim_ticket",
                "server": "task_center",
                "call_via": "python -m task_center --db <中心库> claim_ticket",
                "args": {
                    "project_id": project_id,
                    "ticket_id": ticket_id,
                    "expected_revision": expected_revision,
                    "coordinator_session_id": "<协调会话 id，用 get_current_session_id 取>",
                    "coordinator_workdir": workspace,
                },
                "check": "回执带 attempt_id；把它和工单号一起写进下一条消息。",
            },
            _send_step(
                3, workspace, f"执行工单 {ticket_id}",
                f"你是工单 {ticket_id} 的执行会话。先跑 `python tools/work_gate.py --list` 确认这张单已领取，"
                f"再用 bind_session 绑定自己的 session_id（这一步只有执行者能做）；随后按 PRD 与验收条件执行，"
                f"提交时用 submit_result 并填 `git rev-parse HEAD` 的 40 位 sha。",
                agent_kind, model,
            ),
            _list_sessions_step(4, workspace),
        ],
    }
    return {"bootstrap": bootstrap, "dispatch_ticket": dispatch}


def render_text(plans: dict, *, workspace: str, dry_run: bool) -> str:
    lines = [
        "AI 工作区 · 启动计划",
        f"工作区：{workspace}",
        "调用方式：cindy_mcp_call_tool(server=\"cindy_helper\", tool=\"call_tool\", "
        "args={\"name\": <工具名>, \"args\": <参数>})",
        "预演：只打印计划，不写文件、不调宿主。" if dry_run else "这一步不会自己执行；照计划逐条调，失败按给出的停点处理。",
        "",
        "== bootstrap（装完 → 加成项目 → 开协调会话）",
    ]
    for step in plans["bootstrap"]["steps"]:
        lines.append(f"{step['order']}. {step['tool']}  [{step['server']}]")
        lines.append(f"   参数：{json.dumps(step['args'], ensure_ascii=False)}")
        lines.append(f"   核对：{step['check']}")
    lines += ["", "== dispatch_ticket（claim 之后 → 派发执行会话）"]
    for step in plans["dispatch_ticket"]["steps"]:
        lines.append(f"{step['order']}. {step['tool']}  [{step['server']}]")
        lines.append(f"   参数：{json.dumps(step['args'], ensure_ascii=False)}")
        lines.append(f"   核对：{step['check']}")
    lines += ["", "== 失败停点"]
    for plan_name, failures in FAILURES.items():
        for tool, advice in failures.items():
            lines.append(f"- {plan_name}/{tool}：{advice}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="打印 AI 工作区的启动调用计划")
    parser.add_argument("--workspace", default=str(HERE), help="工作区绝对路径（默认本脚本所在目录）")
    parser.add_argument("--project-name", default=None, help="宿主项目名（默认目录名）")
    parser.add_argument("--title", default="AI 工作区 · 协调会话", help="协调会话标题")
    parser.add_argument("--agent-kind", choices=("claude-code", "codex", "pi"), default=None,
                        help="执行会话的 Agent（缺省继承当前任务）")
    parser.add_argument("--model", default=None, help="执行会话的精确模型 id（缺省继承当前任务）")
    parser.add_argument("--project-id", default="ai-workspace", help="派发计划里的中心库项目 id")
    parser.add_argument("--ticket-id", default="<工单号>", help="派发计划里的工单号")
    parser.add_argument("--expected-revision", type=int, default=1, help="派发计划里的工单 revision")
    parser.add_argument("--json", action="store_true", help="输出机器可读的完整计划")
    parser.add_argument("--dry-run", action="store_true", help="预演：零副作用（本命令本来就不写文件）")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser()
    if not workspace.is_dir():
        print(f"工作区目录不存在：{workspace}", file=sys.stderr)
        return 2
    workspace = workspace.resolve()

    plans = build_plans(
        str(workspace),
        project_name=args.project_name,
        title=args.title,
        agent_kind=args.agent_kind,
        model=args.model,
        project_id=args.project_id,
        ticket_id=args.ticket_id,
        expected_revision=args.expected_revision,
    )
    payload = {
        "ok": True,
        "workspace": str(workspace),
        "dry_run": bool(args.dry_run),
        "side_effects": [],
        "call_via": "cindy_mcp_call_tool",
        "cindy_helper_tools": list(CINDY_HELPER_TOOLS),
        "plans": plans,
        "failure_paths": FAILURES,
        "first_instruction": FIRST_INSTRUCTION,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_text(plans, workspace=str(workspace), dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
