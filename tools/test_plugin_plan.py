#!/usr/bin/env python
"""源码仓库根入口：转到 `kit/tools/test_plugin_plan.py` 执行。

与 `tools/run_tests.py`、`tools/doc_gate.py`、`tools/test_plugin_pack.py` 同一层理由：
插件源码在仓库根的 `plugin/`，而比对用的 `onboard.build_plans()` 与用例在 `kit/` 下。
验收命令写成 `python tools/test_plugin_plan.py` 时在仓库根跑这一层就够；装出来的
工作区不带仓库根的 `tools/` 这一层，也不需要它。
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent / "kit"
sys.path.insert(0, str(KIT))
os.chdir(KIT)
sys.argv[0] = str(KIT / "tools" / "test_plugin_plan.py")
runpy.run_path(sys.argv[0], run_name="__main__")
