#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开工回执的只读校验器：形状过关就 0，三类不合格各报各的、一律非零退出。

为什么要有这个脚本：`onboard.py` 的 bootstrap 计划最后一步让会话在本地写一份
`<workspace>/.data/onboard-receipt.json`。写了没有、写对没有，只有能跑的命令才说得清；
靠人「看一眼觉得写了」就是没有回执。所以这里只读不写，把三种最常见的坏法分开报：

- `receipt missing`：文件不在；
- `… is not absolute`：`project_path` / `working_dir` 不是绝对路径；
- `target_session_id is empty`：会话标识空着（等于没说这是哪个会话）。

三条合起来只印一句「校验失败」的话，看回执的人分不出是没写还是写错了，所以一条一类。

**它证明不了什么**：回执是一份声称，格式合法也可以是编的。真实反查（宿主里那个会话
到底在不在）要靠 `list_sessions`，不在这条命令的范围里。所以通过时原文打印
「形状通过，未验证会话存在」。

    python verify/verify_receipt.py                       # 默认查 ./ 下的 .data/onboard-receipt.json
    python verify/verify_receipt.py --workspace .         # 同上，写成显式
    python verify/verify_receipt.py --workspace D:/proj   # 查某个工作区

退出码：0 形状通过；1 回执缺失或不合格；2 用法错误（--workspace 不是目录）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

RECEIPT_RELATIVE = Path(".data") / "onboard-receipt.json"
# 四个字段与 `kit/onboard.py` 的 RECEIPT_FIELDS 同一份口径；改字段要两边一起改。
REQUIRED_FIELDS = ("project_path", "target_session_id", "working_dir", "created_at")
PATH_FIELDS = ("project_path", "working_dir")
SHAPE_ONLY_NOTE = "形状通过，未验证会话存在"


def receipt_path(workspace: Path) -> Path:
    return workspace / RECEIPT_RELATIVE


def problems_in(data) -> list:
    """按字段逐条报，不合并：合并了就看不出是哪个字段坏。"""
    if not isinstance(data, dict):
        return [f"receipt malformed: 顶层不是 JSON 对象（{type(data).__name__}）"]
    problems = []
    for field in REQUIRED_FIELDS:
        if field not in data:
            problems.append(f"{field} is missing")
            continue
        value = data[field]
        if not isinstance(value, str):
            problems.append(f"{field} is not a string（{type(value).__name__}）")
            continue
        if not value.strip():
            problems.append(f"{field} is empty")
            continue
        if field in PATH_FIELDS and not os.path.isabs(value):
            problems.append(f"{field} is not absolute: {value}")
    return problems


def inspect(workspace: Path) -> list:
    path = receipt_path(workspace)
    if not path.is_file():
        return [f"receipt missing: {path}"]
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as problem:
        return [f"receipt unreadable: {path}（{problem.strerror or problem}）"]
    try:
        data = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError as problem:
        return [f"receipt unreadable: {path}（{problem}）"]
    return problems_in(data)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="校验 <workspace>/.data/onboard-receipt.json 的形状")
    parser.add_argument("--workspace", default=".", help="工作区目录（默认当前目录）")
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).expanduser()
    if not workspace.is_dir():
        print(f"--workspace 不是目录：{workspace}", file=sys.stderr)
        return 2
    workspace = workspace.resolve()
    problems = inspect(workspace)
    if problems:
        print(f"receipt invalid: {receipt_path(workspace)}", flush=True)
        for line in problems:
            print(f"- {line}", flush=True)
        return 1
    print(f"receipt ok: {receipt_path(workspace)}")
    print(SHAPE_ONLY_NOTE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
