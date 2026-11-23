#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把「无人值守跑起来」这件事变成一条命令：输出 `schedule_create` 的完整参数。

为什么要有这个脚本：调度正文是判断，参数是机械的。本机那两条调度（队列值守、无人
值守派单器）跑起来之后，想在自己环境里复制一遍的人要读一遍正文、再自己拼 cron、
timezone、silentWhenIdle 与 preRunHook——拼错哪一项都不会报错，只会静悄悄地不干活。

所以这里只做一件事：**把模板填成参数，打印出来**。

它**不建调度**。`schedule_create` 是宿主能力，建一条调度等于在别人的机器上写状态，
还要人确认模型、目录与静默策略。脚本连这个念头都不该有：没有 import 任何宿主 SDK，
没有写文件，没有调 `schedule_*`。真建调度由人带着 agent 调一次，参数从这里拿。

两条调度的闸门不一样，不是漏了：

- **派单器有 `preRunHook`**：`python <工作区>/tools/next_action.py`。它的退出码本来就是
  「有没有活」的判据（0 有活 / 2 没活 / 1 出错），而宿主的 preRunHook 协议是
  0 放行、2 跳过本轮、其它 fail-closed——两套口径一致，所以直接把它当钩子，不再包一层
  脚本。库读不开时钩子退出 1，本轮被拦住并记失败，不会安安静静地当成「没活」。
- **队列值守没有钩子**：它要看的是「会话卡住了」，而这件事恰恰不在工单库里——
  `next_action.py` 说「没活」的时候，正是会话挂在那儿没人管的时候。给它加闸门会把
  唯一能发现这种情况的一路关掉，所以宁可每 30 分钟醒一次。

用法（在仓库根或装出来的工作区里都能跑）：

    python tools/schedule_plan.py --workspace D:/tmp/ws
    python tools/schedule_plan.py --workspace D:/tmp/ws --json
    python tools/schedule_plan.py --workspace D:/tmp/ws --only dispatch --json

退出码：0 打印了参数；2 参数不合法或缺模板。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 模板目录：脚本在 <工作区>/tools/ 下，模板在 <工作区>/automation/ 下。
HERE = Path(__file__).resolve().parent
AUTOMATION = HERE.parent / "automation"

# 模板里的占位符（双花括号变量）。装好的工作区里这些已经被 install.py 换成真实路径，
# 源码树里还是占位；两种布局都要能跑，所以替换是幂等的。
PLACEHOLDER = re.compile(r"\{\{\s*WORKSPACE\s*\}\}")

# 模板里不许留下的本机绑定物。这里只是**自查**：脚本输出的东西会被贴进别人的宿主，
# 里面混进这台机器的调度 id、会话 id 或会话库路径，等于把一台机器的账本发出去。
HOST_BINDINGS = (
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
     "36 位 id（调度 / 会话 / 尝试）"),
    (re.compile(r"cindy-[a-z0-9]+\.db", re.I), "宿主会话库路径"),
    (re.compile(r"targetSessionId"), "绑死到某条会话的 targetSessionId"),
    (re.compile(r"\b(deepseek|claude-[a-z0-9]|x-ai/|chatgpt/)", re.I), "模型 id"),
)


def template_path(name: str) -> Path:
    return AUTOMATION / f"{name}.md"


def schedule_defs() -> list[dict]:
    """两条调度的静态部分。prompt 现填，其余写在这里，免得散在文档里。"""
    return [
        {
            "key": "queue",
            "template": "queue-watch",
            "name": "队列值守 · 每30分钟巡一遍 {workspace_name}",
            "cronExpr": "0,30 * * * *",
            "policy": "每 30 分钟醒一次，扫最近活跃的项目目录与会话；没有要介入的就静默结束。",
        },
        {
            "key": "dispatch",
            "template": "dispatcher",
            "name": "无人值守派单器 · {workspace_name}",
            "cronExpr": "*/10 * * * *",
            "policy": "每 10 分钟问一次 next_action.py 该做什么，一次只推进一格；闸门是它的退出码。",
        },
    ]


def normalize_workspace(raw: str) -> str:
    """工作区路径统一成正斜杠、不带尾斜杠。

    不调 `resolve()`：`D:/tmp/ws` 在 Windows 上会被解成 `D:\\tmp\\ws`，而模板里的路径
    是给人读的、也常被粘到别处，正斜杠更稳。也不做存在性检查——这里输出的是参数，
    不是去读那个目录。
    """
    text = str(raw).strip().replace("\\", "/")
    while len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text


def render_prompt(template: str, workspace: str) -> str:
    """把模板里的工作区路径换成目标路径；换不干净就报错，不输出半成品。

    两种布局都要认：源码树里的模板带占位符，`install.py` 装完之后占位符已经换成
    「装到哪个工作区」的路径。装出来的那一份如果只认占位符，`--workspace` 就形同
    虚设：prompt 里还是安装时的那个目录，参数看着对、跑起来却指着别处。所以先把
    占位符换掉，再把「本工作区自己那一串路径」也换掉（正反斜杠两种写法都算）。
    """
    body = PLACEHOLDER.sub(workspace, template)
    here = str(HERE.parent).replace("\\", "/")
    if here and here != workspace:
        body = body.replace(here, workspace)
        body = body.replace(here.replace("/", "\\"), workspace)
    if PLACEHOLDER.search(body):
        raise ValueError("模板里还有没替换的占位符")
    return body


def leftovers(text: str) -> list[str]:
    """模板正文里还剩下的本机绑定物，按「是什么」列出来。"""
    return [what for pattern, what in HOST_BINDINGS if pattern.search(text)]


def command_for(workspace: str) -> str:
    """`preRunHook` 要跑的命令：直接跑 next_action.py，退出码就是判据。"""
    return f'python "{workspace}/tools/next_action.py"'


def build(workspace: str, timezone: str, only: str | None = None) -> list[dict]:
    """拼出 `schedule_create` 的参数。真正的建调度不在这里，也不该在这里。"""
    ws = normalize_workspace(workspace)
    workspace_name = Path(ws).name or ws
    result = []
    for item in schedule_defs():
        if only and item["key"] != only:
            continue
        path = template_path(item["template"])
        if not path.is_file():
            raise FileNotFoundError(f"缺模板：{path}")
        template = path.read_text(encoding="utf-8")
        found = leftovers(template)
        if found:
            raise ValueError(f"模板 {path.name} 里有本机绑定物：{'、'.join(found)}")
        entry = {
            "name": item["name"].format(workspace_name=workspace_name),
            "cronExpr": item["cronExpr"],
            "timezone": timezone,
            "recurring": True,
            "workingDir": ws,
            "prompt": render_prompt(template, ws),
            "silentWhenIdle": True,
            "preRunHook": (
                {"command": command_for(ws), "timeoutMs": 90000}
                if item["key"] == "dispatch"
                else None
            ),
            "template": f"automation/{item['template']}.md",
            "policy": item["policy"],
        }
        result.append(entry)
    return result


def main(argv=None) -> int:
    try:  # Windows 控制台默认 GBK，中文一打印就 UnicodeEncodeError（JSON 也就解析不了）
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(
        prog="schedule_plan.py",
        description="输出 schedule_create 的完整参数；只打印，不建调度。",
    )
    parser.add_argument("--workspace", required=True,
                        help="已装好的工作区绝对路径，模板里的占位符换成它。")
    parser.add_argument("--timezone", default="Asia/Shanghai",
                        help="IANA 时区 id，缺省 Asia/Shanghai。")
    parser.add_argument("--only", choices=("queue", "dispatch"),
                        help="只输出其中一条。缺省两条都输出。")
    parser.add_argument("--json", action="store_true", help="机器可读的 JSON。")
    args = parser.parse_args(argv)

    ws = normalize_workspace(args.workspace)
    if not Path(ws).is_absolute():
        print(f"× --workspace 必须是绝对路径，收到的是 {args.workspace!r}", file=sys.stderr)
        return 2

    try:
        schedules = build(ws, args.timezone, args.only)
    except (FileNotFoundError, ValueError) as error:
        print(f"× {error}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "ok": True,
            "workspace": ws,
            "note": "以下每一项都是 schedule_create 的 args；本脚本不建调度。",
            "schedules": schedules,
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"工作区：{ws}")
    print("这些是 schedule_create 的 args；本脚本不建调度，建不建、什么时候建由人定。")
    for item in schedules:
        hook = item["preRunHook"]["command"] if item["preRunHook"] else "（不设闸门）"
        print("")
        print(f"· {item['name']}")
        print(f"  cronExpr        {item['cronExpr']}  ({item['timezone']})")
        print(f"  silentWhenIdle  {str(item['silentWhenIdle']).lower()}")
        print(f"  workingDir      {item['workingDir']}")
        print(f"  preRunHook      {hook}")
        print(f"  模板            {item['template']}")
        print(f"  节奏            {item['policy']}")
    print("")
    print("建法见 docs/无人值守派单.md 的「在自己的 Cindy 里建这条调度」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
