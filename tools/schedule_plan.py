#!/usr/bin/env python
"""源码仓库根入口：转到 `kit/tools/schedule_plan.py` 执行。

与 `tools/run_tests.py` / `tools/doc_gate.py` 同一层理由：套件的正文与工具住在 `kit/` 下，
`install.py` 会把它们复制成工作区根；所以在仓库根敲 `python tools/schedule_plan.py` 时，
这一层只负责把参数原样转给 `kit/tools/schedule_plan.py`。

装出来的工作区里没有这个文件——那里 `tools/schedule_plan.py` 就是真的那一份。
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent / "kit"
sys.path.insert(0, str(KIT))
os.chdir(KIT)
sys.argv[0] = str(KIT / "tools" / "schedule_plan.py")
runpy.run_path(sys.argv[0], run_name="__main__")
