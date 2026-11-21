#!/usr/bin/env python
"""源码仓库根入口：转到 `kit/tools/doc_gate.py` 执行。

与 `tools/run_tests.py` 同一层理由：套件仓库的工作区内容在 `kit/` 下，
验收命令写成 `python tools/doc_gate.py ...` 时在仓库根跑这一层就够；
装出来的工作区不需要它。参数原样转发。
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent / "kit"
sys.path.insert(0, str(KIT))
os.chdir(KIT)
sys.argv[0] = str(KIT / "tools" / "doc_gate.py")
runpy.run_path(sys.argv[0], run_name="__main__")
