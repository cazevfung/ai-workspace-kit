#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动层：把「装完之后怎么在宿主里开工」打印成一份能照着调的调用计划。

为什么要有这个脚本：`install.py` 只把文件铺到磁盘上，装完那一刻用户看到的是一堆
命令，而不是「下一步点哪里、传什么参数」。宿主能力（创建项目、开协调会话）不在
安装器里，也不该由安装器偷偷调用——它们是宿主操作，要显示在会话里、失败要能看见。
所以这里只产出计划：每一步用哪个工具、完整参数是什么、怎么反查、失败时停在哪。

    python onboard.py                  # 打印三步的人读计划
    python onboard.py --json           # 每步完整参数，供技能与插件复用
    python onboard.py --host generic   # 非 Cindy 宿主：输出手工计划，不含任何宿主工具
    python onboard.py --dry-run        # 预演：仍然只打印，不写任何文件、不调宿主
    python onboard.py --json --dry-run # 预演 + JSON

两条计划同源：

- `bootstrap`：list_projects → create_project → send_to_session → list_sessions →
  write_receipt（`local` 步），对应「装完 → 把工作区加成项目 → 开协调会话 → 发第一条
  指令 → 反查会话 → 本地写回执」。最后一步不是宿主工具：它由本地写文件完成，
  写 `<workspace>/.data/onboard-receipt.json`，四个字段见 `RECEIPT_FIELDS`。
- `dispatch_ticket`：get_ticket → claim_ticket → send_to_session → list_sessions，
  派发一张工单时用；其中 send_to_session / list_sessions 与 bootstrap 同一份参数。

两种宿主，两条路（`--host` 决定用哪条）：

- `cindy`（默认）：有 `create_project` / `send_to_session` / `schedule_create`，
  上面两张计划照旧可用。
- `generic`：Claude Code、Codex、手工终端这类宿主只有文件、脚本与 cron。没有
  `send_to_session`，所以计划换成 `generic_dispatch`：create_ticket → claim_ticket →
  open_session（`local` 步，人或宿主自己开）→ bind_session → submit_result →
  get_ticket（反查绑定）。**没有宿主建会话能力时就停在 open_session 那一步，不许拿猜的
  session_id 顶替**——2026-11-17 AW-007：从临时目录名拼了一个 session id，库照收，
  `list_sessions` 里根本没有那条会话。同一条规矩也写在 `check_session_id()` 里：它只收
  UUID 形状，把目录名 slug 化出来的串（`session_D_AI`、`D_AI工作区`）也挡在外面。
  它**只核形状、不核存在**——形状对了也要靠第 6 步 `get_ticket` 反查那条会话真的在执行。

两份 payload 不同源（2026-11-24 返工）：`--host generic` 的 JSON 里没有 `cindy_helper_tools`
这个键，`failure_paths` 只有 `generic_dispatch` 一个分区。bootstrap / dispatch_ticket 的停点
全是 Cindy 专用，混进 generic 的 payload 就与 `host_capabilities.create_project=false` 打架。

退出码：0 打印成功；2 参数不合法（工作区目录不存在、`--session-id` 不是 UUID 形状或配错宿主）。

依据：PRD-001 v0.1 的闭环形状与「归属不猜」；AGENTS.md 硬规则五（派发是新开一个
会话，执行者自己 bind_session）。
"""
from __future__ import annotations

import argparse
import json
import re
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
TASK_CENTER_TOOLS = ("get_ticket", "claim_ticket", "create_ticket", "bind_session", "submit_result")
# `local` 步：不经过宿主，由会话在本地写文件。它故意不在上面两张清单里——
# 混进去就等于声称宿主有这个工具。
LOCAL_STEP_KIND = "local"
# `open_session` 同理：非 Cindy 宿主没有建会话的工具，这一步只能是人或宿主自己做。
OPEN_SESSION_TOOL = "open_session"
HOSTS = ("cindy", "generic")
# 非 Cindy 宿主要求「真实 session id」而不是猜一个：2026-11-17 AW-007 的分界线。
SESSION_ID_PLACEHOLDER = "<真实 session_id：第 3 步宿主返回的那个，原样填写，不许猜>"
SESSION_ID_LIMIT = 500  # 与 bind_session 的 text(data, "session_id", 500) 一致
SESSION_ID_REJECT_MARKERS = ("placeholder", "占位", "unknown", "todo", "tbd",
                             "guess", "猜", "fake", "dummy", "example", "sample")
# 2026-11-24 返工（AWK-106 C4）：只挡占位词与路径分隔符不够。`session_D_AI`、
# `D_AI工作区` 这类「把目录名 slug 化」的串既没有分隔符、也没有占位词，旧判据照样放行，
# 一路写进 bind_session。所以判据改成正面的形状：只收 UUID，允许带前缀。
# 本仓库里的真实会话 id 都是这个形状；宁可拒收，也不放一个看起来像 id 的目录名进去。
SESSION_ID_SHAPE = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9_.]*[_:-])?"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
SESSION_ID_SHAPE_TEXT = "8-4-4-4-12 位小写十六进制的 UUID，可带前缀（如 session_<uuid>）"
SESSION_ID_SHAPE_ADVICE = ("本仓库里的真实会话 id 都是 UUID；你的宿主若不是这个形状，"
                           "就在计划里写『此处由你填写真实 id』，不要猜、不要从路径拼。")
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
        "get_ticket": "工单不是 planned / ready / rework → 不要重复领取；先看 state 说明它停在哪一步。",
        "claim_ticket": "DISPATCH_BUSY → 同项目已有在途单，先做完那张；SELF_DISPATCH → 换一个执行会话，"
                        "不要协调会话自己做；STALE_ATTEMPT → 重新 get_ticket 取当前 revision。",
        "send_to_session": "返回 NOT_FOUND / ARCHIVED / DELETED → 不要换一个字符串冒充执行者；先修会话再重派。"
                           "返回 created 但标题不是本单 → 回执发错了地方，停掉误建任务再带 target_session_id 重发。",
        "list_sessions": "找不到 title/workingDir 匹配的执行会话 → 派发未完成；不要自己 bind_session 顶替执行者。",
    },
    "generic_dispatch": {
        "create_ticket": "CRITERIA_NOT_JUDGEABLE → 每条判据要么「命令 + 期望输出」、要么「判断类 + 反例」，"
                         "缺一条就不开单（一个字段不写进库）；NOT_FOUND → 先 add_project 把项目接进来；"
                         "号由库里发：不要自己填 ticket_id。",
        "claim_ticket": "DISPATCH_BUSY → 同项目已有在途单，先做完那张；BLOCKED_BY_DEPENDENCY → 前置单还没 done。"
                        "拿不到宿主自己给的会话标识就停在这一步：不要用目录名、工单号或任何路径拆一个字符串顶替协调者。",
        "open_session": "没有宿主建会话能力就停在这里：不许拿临时目录名、运行目录、工单号或任何路径拆出来的字符串"
                        "顶替真实 session_id（2026-11-17 AW-007：从临时目录名拼了一个 session id，库照收，"
                        "list_sessions 里根本没有那条会话）。拿不到真实 id 就不 bind，也不继续 submit。",
        "bind_session": "PROJECT_MISMATCH → project_root 与库里登记的项目根不一致，先回第 3 步核项目；"
                        "SELF_DISPATCH → 你绑的是协调会话自己，回去开一个独立的执行会话；"
                        "STALE_ATTEMPT → 重新 get_ticket 取当前 attempt。不许改传一个猜的 session_id 重试。",
        "submit_result": "head_sha / base_sha 不是 40 位十六进制 → 用 git rev-parse HEAD 与 git merge-base HEAD master 的输出；"
                         "ARTIFACT_CHANGED → 成果被改、删或移出项目，重新提交；"
                         "STALE_PRD → PRD 改过，走 revise_ticket，不要直接重交。",
        "get_ticket": "state 不是 running，或 attempts[-1].session_id 与第 3 步那个不一致 → 绑定没成，"
                      "不要说已经开工；先看 attempts[*].session_id 再决定重绑还是重开会话。",
    },
}


def check_session_id(value) -> tuple[bool, str]:
    """一个 session 标识能不能进 bind_session。纯函数，不改任何东西。

    为什么要这一道：2026-11-17 AW-007 从临时目录名拼了一个 id，`bind_session` 照收，
    `list_sessions` 里根本没有那条会话。库能收不等于会话存在，所以把「看起来就是猜的」
    挡在计划层。

    判据是正面的形状：**只收 UUID**（8-4-4-4-12 小写十六进制，允许带前缀），其余一律拒。
    空串、占位符、路径分隔符、占位词各自给出原因；`session_D_AI` / `D_AI工作区` 这种把
    目录名 slug 化出来的形态既没有分隔符也没有占位词，只有形状判据拦得住。

    **只核形状、不核存在**：形状合法的随手 id 也过得了这道闸门，它在宿主里到底有没有，
    只能靠第 6 步 `get_ticket` 反查——所以计划里不写「已验证存在」。

    返回 `(能不能用, 不能用的原因)`；能用时原因为空串。
    """
    if not isinstance(value, str):
        return False, "不是字符串"
    text = value.strip()
    if not text:
        return False, "是空的：bind_session 需要宿主真正返回的那个标识"
    if len(text) > SESSION_ID_LIMIT:
        return False, f"有 {len(text)} 字符，超过 bind_session 的 {SESSION_ID_LIMIT} 字符上限"
    if any(mark in text for mark in "<>{}"):
        return False, "还留着占位符：这不是宿主返回的标识"
    lowered = text.lower()
    for marker in SESSION_ID_REJECT_MARKERS:
        if marker in lowered:
            return False, f"含有占位或猜测的字样（{marker}）：这不是宿主返回的标识"
    if "/" in text or "\\" in text:
        return False, "含路径分隔符：从目录名或路径拆出来的字符串不是会话标识"
    if not SESSION_ID_SHAPE.match(text):
        shown = text if len(text) <= 60 else text[:57] + "..."
        return False, (f"不是 UUID 形状（只收 {SESSION_ID_SHAPE_TEXT}，收到的是「{shown}」）："
                       + SESSION_ID_SHAPE_ADVICE)
    return True, ""


def _generic_open_step(order: int) -> dict:
    """第 3 步：人或宿主自己开一个执行会话。它没有工具可调，所以标 local。"""
    return {
        "order": order,
        "tool": OPEN_SESSION_TOOL,
        "server": "local",
        "kind": LOCAL_STEP_KIND,
        "call_via": "人或宿主自己做：另开一个终端会话 / 用宿主自己的新建命令开一个任务 / 开一次性子任务",
        "args": {
            "who": "一个独立的执行会话（不能是当前这条协调会话）",
            "how": ["人在另一个终端里开一个会话",
                    "用宿主自己的「新建会话」命令或快捷键开一个任务",
                    "宿主的一次性子任务（必须在 submit_result 的 evidence 里注明「无法追问」）"],
            "must_return": "宿主自己报出的 session id：新会话自己报的、宿主回执里的、或宿主会话列表里那一条的 id",
            "must_not_return": "临时目录名、运行目录、工单号或任何从路径拆出来的字符串；也不接受把它们 slug 化出来的串（session_D_AI、D_AI工作区）",
        },
        "check": "拿到的是宿主自己报出的真实 session id，并能在宿主里再查一次看到这条会话；"
                 "拿不到就停在这一步，不许拿猜的 session_id 顶替。",
        "shape_only": "onboard.check_session_id 只核形状（UUID）、不核存在：闸门放行不等于这条会话真的在宿主里。",
    }


def _generic_plan(workspace: str, *, project_id: str, ticket_id: str,
                  expected_revision: int, session_id: str | None = None) -> dict:
    """非 Cindy 宿主的派单计划：整张计划里一个宿主工具都没有。"""
    bind_session = session_id or SESSION_ID_PLACEHOLDER
    steps = [
        {
            "order": 1,
            "tool": "create_ticket",
            "server": "task_center",
            "call_via": "python -m task_center --db <中心库> create_ticket --request-id <唯一编号> --input <JSON文件>",
            "args": {
                "project_id": project_id,
                "title": "<工单标题：一句话说清这单要改什么>",
                "deliverable": "<主题 + 明确不覆盖什么>",
                "prd_path": "<项目内的 PRD 相对路径>",
                "criteria": ["<命令 + 期望输出，或「判断类」+ 反例；至少一条>"],
            },
            "check": "回执里的 ticket_id 是库里发的号，不要自己编；没有现成工单时从这一步开始，已有工单就跳到第 2 步。",
        },
        {
            "order": 2,
            "tool": "claim_ticket",
            "server": "task_center",
            "call_via": "python -m task_center --db <中心库> claim_ticket --request-id <唯一编号> --input <JSON文件>",
            "args": {
                "project_id": project_id,
                "ticket_id": ticket_id,
                "expected_revision": expected_revision,
                "coordinator_session_id": "<宿主自己给的会话标识：当前这条会话的那个，向宿主取>",
                "coordinator_workdir": workspace,
            },
            "check": "回执带 attempt_id 与 dispatching；拿不到宿主自己给的会话标识就停在这一步——"
                     "不要用路径名或工单号顶替协调者。",
        },
        _generic_open_step(3),
        {
            "order": 4,
            "tool": "bind_session",
            "server": "task_center",
            "call_via": "python -m task_center --db <中心库> bind_session --request-id <唯一编号> --input <JSON文件>",
            "args": {
                "project_id": project_id,
                "ticket_id": ticket_id,
                "attempt_id": "<第 2 步回执里的 attempt_id>",
                "session_id": bind_session,
                "project_root": workspace,
            },
            "requires_real_session_id": True,
            "check": "session_id 必须是第 3 步里宿主报出的真实标识，原样填写；没有真实标识就不 bind，"
                     "不许猜、也不许从目录名拼，slug 化过的一样不行。闸门只核形状、不核存在；"
                     "回执的 session_id 与本步传入的一字不差才算绑上；"
                     "PROJECT_MISMATCH 回第 3 步核项目根，SELF_DISPATCH 说明你开的是同一条会话。",
        },
        {
            "order": 5,
            "tool": "submit_result",
            "server": "task_center",
            "call_via": "python -m task_center --db <中心库> submit_result --request-id <唯一编号> --input <JSON文件>",
            "args": {
                "project_id": project_id,
                "ticket_id": ticket_id,
                "attempt_id": "<第 2 步回执里的 attempt_id>",
                "session_id": bind_session,
                "summary": "<成果摘要>",
                "artifacts": [f"{workspace}/<成果文件>", f"{workspace}/<自检记录>"],
                "evidence": ["<跑过的命令与输出摘要>"],
                "risks": ["<已知风险；没有就写空数组>"],
                "head_sha": "<git rev-parse HEAD 的 40 位十六进制>",
                "base_sha": "<git merge-base HEAD master 的 40 位十六进制>",
            },
            "check": "提交时用第 4 步同一个 session_id；head_sha 是被验收的那棵树，base_sha 是它分叉自的 commit，"
                     "两个都不是「填表那一刻 master 的 sha」。",
        },
        {
            "order": 6,
            "tool": "get_ticket",
            "server": "task_center",
            "call_via": "python -m task_center --db <中心库> get_ticket --request-id <唯一编号> --input <JSON文件>",
            "args": {"project_id": project_id, "ticket_id": ticket_id},
            "check": "非 Cindy 宿主没有 list_sessions 可查，就用这一下反查：state 是 running / awaiting_review，"
                     "且 attempts[-1].session_id 与第 3 步拿到的那个一字不差，才算这条会话真的在执行这张单。",
        },
    ]
    return {
        "purpose": "非 Cindy 宿主（Claude Code / Codex / 手工终端）派一张单：库里开单、领取、由人或宿主开执行会话、"
                   "执行者自己绑定、提交成果。",
        "host": "generic",
        "host_capabilities": {
            "create_project": False,
            "send_to_session": False,
            "schedule_create": False,
            "files_scripts_cron": True,
        },
        "stop_points": [
            "没有宿主建会话能力时就停在第 3 步（open_session）：不许拿猜的 session_id 顶替。"
            "2026-11-17 AW-007：从临时目录名拼了一个 session id，库照收，list_sessions 里根本没有那条会话。",
            "第 2 步拿不到宿主自己给的会话标识就停：协调者的标识也只能问宿主，不许拼。",
            "第 4 步 bind_session 的 session_id 只能用第 3 步宿主报出的那个；"
            "校验由 onboard.check_session_id 做，只收 UUID 形状，猜的值与目录名 slug 在计划层就被拒。",
            "这道闸门只核形状、不核存在：形状对也不等于宿主里真有这条会话，"
            "所以计划里不写「已经存在」这类话，只写「必须来自宿主回执或用户」。",
            "第 6 步 get_ticket 里 state 与 session_id 对不上，就是绑定没成：不要说已经开工。",
        ],
        "session_identity": {
            "rule": "执行会话的 session_id 必须来自宿主自己（新会话自报、宿主回执、或宿主会话列表）；"
                    "协调会话的标识也必须向宿主取。禁止从临时目录名、运行目录或任何路径拆一个字符串当标识。",
            "shape": f"只收 UUID：{SESSION_ID_SHAPE_TEXT}",
            "does_not_check": "闸门只核形状、不核存在。这个 id 必须来自宿主回执或用户；"
                              "形状合法也不代表宿主里有这条会话，存在性只能靠第 6 步 get_ticket 反查。",
            "source": "命令行给出" if session_id else "第 3 步由宿主报出",
            "reject": ["空串",
                       "不是 UUID 形状（含把目录名 slug 化出来的串，如 session_D_AI、D_AI工作区）",
                       "含 < > { } 的占位符", "含路径分隔符",
                       "含 placeholder/占位/unknown/todo/tbd/guess/猜 等字样", f"超过 {SESSION_ID_LIMIT} 字符"],
            "guard": "onboard.check_session_id",
        },
        "must_not": "没有宿主建会话能力时就停在第 3 步，不许拿猜的 session_id 顶替；"
                     "拿不到真实标识就不 bind_session，也不提交成果。",
        "steps": steps,
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
                expected_revision: int = 1, host: str = "cindy",
                session_id: str | None = None) -> dict:
    """返回该宿主的计划。纯函数：不读环境、不写文件、不调宿主。

    `host="cindy"`（默认）返回 bootstrap + dispatch_ticket 两张；`host="generic"`
    只返回 generic_dispatch 一张，里面没有任何 cindy_helper 工具。默认值不变是为了
    插件与 verify 对得上：`plugin/draft/main.js` 的 PLANS 比的是默认那张。
    """
    if host not in HOSTS:
        raise ValueError(f"未知宿主：{host}（只收 {' / '.join(HOSTS)}）")
    workspace = str(Path(workspace).expanduser().resolve())
    if host == "generic":
        return {"generic_dispatch": _generic_plan(
            workspace, project_id=project_id, ticket_id=ticket_id,
            expected_revision=expected_revision, session_id=session_id,
        )}
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
                "check": "state 必须是 planned / ready / rework；不是就先看它停在哪一步。",
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


def render_text(plans: dict, *, workspace: str, dry_run: bool, host: str = "cindy") -> str:
    lines = [
        "AI 工作区 · 启动计划",
        f"工作区：{workspace}",
        f"宿主：{host}",
    ]
    if host == "generic":
        lines.append("调用方式：task_center 步骤用 `python -m task_center --db <中心库> <操作> --request-id <唯一编号> "
                     "--input <JSON文件>`；`open_session` 是 local 步，由人或宿主自己开。")
    else:
        lines.append("调用方式：cindy_mcp_call_tool(server=\"cindy_helper\", tool=\"call_tool\", "
                     "args={\"name\": <工具名>, \"args\": <参数>})")
    lines.append("预演：只打印计划，不写文件、不调宿主。" if dry_run
                 else "这一步不会自己执行；照计划逐条调，失败按给出的停点处理。")
    for plan_name, plan in plans.items():
        lines += ["", f"== {plan_name}（{plan['purpose']}）"]
        for step in plan["steps"]:
            lines.append(f"{step['order']}. {step['tool']}  [{step['server']}]")
            lines.append(f"   参数：{json.dumps(step['args'], ensure_ascii=False)}")
            lines.append(f"   核对：{step['check']}")
        for point in plan.get("stop_points", []):
            lines.append(f"停点：{point}")
    lines += ["", "== 失败停点"]
    for plan_name in plans:
        for tool, advice in FAILURES[plan_name].items():
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
    parser.add_argument("--host", choices=HOSTS, default="cindy",
                        help="宿主类型：cindy（有 create_project / send_to_session）或 generic（只有文件、脚本与 cron）")
    parser.add_argument("--session-id", default=None,
                        help="generic 宿主已有真实会话标识时直接填进 bind_session；猜的或占位的值会被拒（退出 2）")
    parser.add_argument("--dry-run", action="store_true", help="预演：零副作用（本命令本来就不写文件）")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser()
    if not workspace.is_dir():
        print(f"工作区目录不存在：{workspace}", file=sys.stderr)
        return 2
    workspace = workspace.resolve()

    session_id = args.session_id
    if session_id is not None:
        if args.host != "generic":
            print("`--session-id` 只用于 `--host generic`：Cindy 的 session id 由 send_to_session 回执给出。",
                  file=sys.stderr)
            return 2
        ok, reason = check_session_id(session_id)
        if not ok:
            print(f"session_id 不可用：{reason}。不许猜，也不许从目录名拼一个；"
                  "拿不到宿主报出的真实标识就停在 open_session 那一步。", file=sys.stderr)
            return 2

    plans = build_plans(
        str(workspace),
        project_name=args.project_name,
        title=args.title,
        agent_kind=args.agent_kind,
        model=args.model,
        project_id=args.project_id,
        ticket_id=args.ticket_id,
        expected_revision=args.expected_revision,
        host=args.host,
        session_id=session_id,
    )
    payload = {
        "ok": True,
        "workspace": str(workspace),
        "dry_run": bool(args.dry_run),
        "host": args.host,
        "side_effects": [],
        "call_via": "cindy_mcp_call_tool" if args.host == "cindy"
                    else "python -m task_center --db <中心库> <操作> --request-id <唯一编号> --input <JSON文件>",
        "plans": plans,
        "failure_paths": FAILURES,
        "first_instruction": FIRST_INSTRUCTION,
    }
    if args.host == "generic":
        # C3（2026-11-24 返工）：`--host generic` 的 payload 里不能有 `cindy_helper_tools` 这个键，
        # `failure_paths` 也只能有 generic_dispatch。bootstrap / dispatch_ticket 的停点全是 Cindy
        # 专用（create_project / send_to_session），留在这份 payload 里等于说「这个宿主有那些步骤」，
        # 与 host_capabilities.create_project=false 自相矛盾。
        # 宿主能力与停点在顶层也放一份：调用方不必先拆 plans 才能答「这个宿主能不能建会话」。
        payload["failure_paths"] = {"generic_dispatch": FAILURES["generic_dispatch"]}
        plan = plans["generic_dispatch"]
        payload.update({
            "host_capabilities": plan["host_capabilities"],
            "stop_points": plan["stop_points"],
            "session_identity": plan["session_identity"],
            "must_not": plan["must_not"],
        })
    else:
        payload["cindy_helper_tools"] = list(CINDY_HELPER_TOOLS)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_text(plans, workspace=str(workspace), dry_run=args.dry_run, host=args.host))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
