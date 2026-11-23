#!/usr/bin/env python3
"""全量测试入口：合并闸门固定跑 `python tools/run_tests.py`。

它只做一件事：把 `tests/` 下**全部** unittest 跑一遍，最后给出一行可读汇总。

- 绿退出 0，红退出非零（闸门只看退出码，红的时候目标分支一动未动）。
- 输出末尾固定一行 `汇总：N 个用例，M 个失败…`；闸门把「汇总：」之后那段写进合并记录。
- 零依赖：只用标准库，不引 pytest；没有写死的绝对路径（仓库根由本文件位置算出），
  所以整棵树拷到临时目录、或在临时工作树里都能直接跑。
- `tests/live_harness.js` 那条用例自己会看本机有没有 node，没有就跳过——这里不干预，
  也不因为跳过而判红。

为什么不直接 `python -m unittest discover -s tests`：这条命令的末行是 `OK` 或
`FAILED (...)`，没有固定形状的总数与失败数，闸门拿不到一行能写进记录的摘要；
但两者用的是同一套发现规则与加载器，用例总数一致（见 `tests/` 与合并记录的核对）。
"""

from __future__ import annotations

import os
import sys
import unittest

# 仓库根 = 本文件所在目录（tools/）的上一级。不写死路径，整个仓库挪到哪都能跑。
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(REPO, "tests")


def _utf8_stdout() -> None:
    """把标准输出固定成 UTF-8。

    闸门用 `subprocess.run(..., encoding="utf-8")` 读我们的输出；Windows 上 Python 往管道
    写东西时默认按控制台代码页（cp936）编码，那样「汇总：」会被读成乱码，闸门就认不出这一行。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def main() -> int:
    _utf8_stdout()

    if not os.path.isdir(TESTS):
        print(f"找不到测试目录：{os.path.relpath(TESTS, REPO)}")
        print("汇总：0 个用例，1 个失败（没有 tests/ 目录）")
        return 2

    # 以仓库根为 cwd：用例里的相对路径、子进程调用都按「在仓库根跑」这一条约定来。
    os.chdir(REPO)
    if REPO not in sys.path:
        sys.path.insert(0, REPO)

    suite = unittest.TestLoader().discover(TESTS, pattern="test*.py")
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(suite)

    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    skipped = len(result.skipped)
    extra = f"，跳过 {skipped}" if skipped else ""
    verdict = "全绿" if result.wasSuccessful() else "有失败"
    print(f"汇总：{total} 个用例，{failed} 个失败（{verdict}{extra}）")

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
