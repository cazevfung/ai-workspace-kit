#!/usr/bin/env python
"""源码仓库根入口：转到 `kit/tools/test_plugin_pack.py` 执行。

与 `tools/run_tests.py`、`tools/doc_gate.py` 同一层理由：套件仓库本身不是装出来的
工作区，`dist/` 与 `plugin/` 都在仓库根，而插件的源码与用例在 `kit/` 下。
验收命令写成 `python tools/test_plugin_pack.py` 时在仓库根跑这一层就够；装出来的
工作区不带 `dist/` 与 `plugin/`，用例在那里自己跳过，也不需要这一层。
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent / "kit"
sys.path.insert(0, str(KIT))
os.chdir(KIT)
sys.argv[0] = str(KIT / "tools" / "test_plugin_pack.py")
runpy.run_path(sys.argv[0], run_name="__main__")
