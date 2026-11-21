#!/usr/bin/env python
"""源码仓库根入口：转到 `kit/tools/run_tests.py` 执行。

套件仓库本身不是装出来的工作区：真正的工作区内容在 `kit/` 下（`install.py` 会把它
复制到目标目录）。验收命令写成 `python tools/run_tests.py` 时，在仓库根跑这一层就
够了；装出来的工作区不需要它，`install.py` 也不会复制它。
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent / "kit"
sys.path.insert(0, str(KIT))
os.chdir(KIT)
sys.argv[0] = str(KIT / "tools" / "run_tests.py")
runpy.run_path(sys.argv[0], run_name="__main__")
