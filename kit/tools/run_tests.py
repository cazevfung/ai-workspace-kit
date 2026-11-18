#!/usr/bin/env python
"""一条命令跑完仓库里两处 Python 测试：tests/ 与 tools/test_*.py。

为什么要有这个脚本：仓库的测试分在两处，`tests/` 放工单核心的用例，
`tools/` 放闸门脚本（doc_gate / work_gate）自己的用例。以前只跑
`python -m unittest discover -s tests`，等于把 tools 下的用例当作不存在——
schema 4 那次改动让 tools 下 7 项变红，却因为没人跑而没被发现（AW-014 才补上）。
所以把「跑完全部测试」做成一条命令，且两处的用例数都打印出来：
只看到总数看不出漏了哪一处。

光「跑完」还不够，还得防「没跑到」的两种样子（AW-016 发现，AW-020 补上）：

1. **目录没了**：以前某处目录不存在时 discover 返回空 suite，脚本打印
   `=== tests/ (0 项) ===` 然后报「全部通过」。把 `tests/` 改名就能骗过闸门。
   现在目录缺失、或发现到 0 项，都直接算这一处失败，并指明是哪一处。
2. **新目录没人知道**：仓库里新长出一处含 `test_*.py` 的目录，却忘了加进
   SUITES，那批用例永远不会被跑到。现在脚本会扫一遍仓库自检，报出这些目录，
   并提示「新增测试目录请加进 SUITES」。

自检本身也有两种误报（AW-021 修掉，都是验收 AW-020 时真实验出来的）：

- **包内子目录被当成未声明**：`tests/sub/__init__.py` + `tests/sub/test_x.py`
  这种目录 discover 会自己走进去跑，用例确实跑到了，却被报成未声明还让退出码变 1。
  现在按「整条包链都有 __init__.py」判断它是否真会被跑到，是就不报。
- **`.agents` 下的脚本测试被当成漏跑**：那里的技能/插件脚本另有开发线与验收节奏，
  不是本仓库主线 suite 的交付单元，漏跑的前提不成立，所以整个目录不扫。

用法（在仓库根 {{WORKSPACE}}）：

    python tools/run_tests.py
    python tools/run_tests.py -v       # 逐个用例名

退出码：全部通过 0；任一处有失败/错误、目录缺失、发现 0 项、或有未声明的测试目录，都是 1。
"""
from __future__ import annotations

import fnmatch
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# tests/ 与 tools/ 都不是包，discover 会把各自目录当成顶层目录；
# 用例里 `from task_center import Center` 需要仓库根在 sys.path 上。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 顺序固定：先仓库主测试，再闸门脚本测试。新增第三处就加在这里。
SUITES = (
    ("tests/", ROOT / "tests", "test_*.py"),
    ("tools/", ROOT / "tools", "test_*.py"),
)

# 自检扫描时跳过的目录名：版本库、其他工作树、隔离试点材料、字节码缓存、
# 本地数据库、编辑器配置。这些地方的 test_*.py 不是本仓库的测试。
#
# `.agents/` 单列一条理由：里面的技能与插件脚本测试（例如
# `.agents/skills/skill-routing/scripts/test_check_routing.py`）属于另一条开发线，
# 有自己的交付单元与验收节奏，不是本仓库主线 suite 的用例。把它们扫进来报「未声明」，
# 说的是一件不成立的事——那不是漏跑，而是不是同一个交付单元。要收它们，
# 该在那条线上挂 SUITES，不是在这里加一行声明替别人的交付背书。
SKIP_DIRS = frozenset(
    {".git", ".worktrees", "_sandbox", "__pycache__", ".data", ".claude", ".agents"}
)


def find_test_dirs(root: Path, skip: frozenset[str] = SKIP_DIRS) -> list[str]:
    """扫出 root 下所有「直接包含 test_*.py 的目录」，返回相对路径（posix，已排序）。

    纯函数，只读文件系统，所以可以直接拿临时目录写测试。
    root 自身命中时记为 "."。
    """
    root = Path(root).resolve()
    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip)
        if any(fnmatch.fnmatch(name, "test_*.py") for name in filenames):
            found.add(Path(dirpath).resolve().relative_to(root).as_posix())
    return sorted(found)


def _reachable_by_discover(root: Path, suite_dir: str, name: str) -> bool:
    """name 是否落在已声明 suite 之下，且 discover 真能顺着包链走进去。

    discover 只在子目录是包（有 `__init__.py`）时往下递归，缺一层就进不去。
    所以「位于 suite 之下」还不够，得整条链上每一层都是包。
    """
    suite = root if suite_dir in ("", ".") else (root / suite_dir).resolve()
    target = (root / name).resolve()
    try:
        rel = target.relative_to(suite)
    except ValueError:
        return False
    current = suite
    for part in rel.parts:
        current = current / part
        if not (current / "__init__.py").is_file():
            return False
    return True


def check_undeclared_suites(root: Path, declared: list[str]) -> list[str]:
    """自检：仓库里有 test_*.py、却没被 SUITES 声明（也确实跑不到）的目录。

    返回未声明目录的相对路径，空列表表示通过。纯函数，好写测试。
    这类目录里的用例永远不会被 run_tests.py 跑到，所以必须当成失败报出来。

    但「没声明」不等于「没跑到」：已声明 suite 之下的包子目录（带 `__init__.py`）
    discover 会自己走进去。那种目录报成未声明是误报，还会让退出码变 1（AW-021）。
    所以先把它算成已被覆盖，只报真正跑不到的那些。
    """
    root = Path(root).resolve()
    declared_dirs = [Path(name).as_posix().rstrip("/") for name in declared]
    return [
        name
        for name in find_test_dirs(root)
        if name.rstrip("/") not in declared_dirs
        and not any(_reachable_by_discover(root, suite, name) for suite in declared_dirs)
    ]


def discover(path: Path, pattern: str) -> unittest.TestSuite:
    """发现某一处的用例。

    目录不存在不再静默返回空 suite：调用方必须先确认目录存在，
    否则「把 tests/ 改名」会被当成 0 项并报「全部通过」（AW-016 的洞）。
    """
    loader = unittest.TestLoader()
    return loader.discover(str(path), pattern=pattern, top_level_dir=str(path))


def run_suites(root: Path, suites, verbosity: int = 1, stream=None) -> int:
    """跑完 suites 里的每一处，返回 0 表示全通过。

    root 与 suites 都是参数（不是直接读全局），这样测试能拿临时目录跑真实流程。
    """
    stream = stream if stream is not None else sys.stdout
    root = Path(root)
    failed = False
    rows: list[tuple[str, int | None, unittest.TestResult | None]] = []

    declared = [Path(path).relative_to(root).as_posix() for _, path, _ in suites]
    undeclared = check_undeclared_suites(root, declared)
    if undeclared:
        print("\n=== 自检：SUITES 覆盖 ===", file=stream, flush=True)
        print("失败：下面这些目录里有 test_*.py，却不在 SUITES 里，其中的用例永远不会被跑到：", file=stream)
        for name in undeclared:
            print(f"  - {name}/", file=stream)
        print("新增测试目录请加进 SUITES（本文件顶部的 SUITES 常量），否则等于没写测试。", file=stream, flush=True)
        failed = True

    for label, path, pattern in suites:
        path = Path(path)
        if not path.is_dir():
            print(f"\n=== {label}（目录缺失）===", file=stream, flush=True)
            print(f"失败：{label} 声明的测试目录不存在：{path}", file=stream)
            print("这一处一个用例都没跑到，不能当作通过。", file=stream, flush=True)
            rows.append((label, None, None))
            failed = True
            continue

        suite = discover(path, pattern)
        total = suite.countTestCases()
        print(f"\n=== {label} ({total} 项) ===", file=stream, flush=True)
        if total == 0:
            print(f"失败：{label} 发现 0 项用例（目录在，但没有任何匹配 {pattern} 的文件）。", file=stream, flush=True)
            rows.append((label, 0, None))
            failed = True
            continue

        result = unittest.TextTestRunner(stream=stream, verbosity=verbosity).run(suite)
        rows.append((label, total, result))
        if not result.wasSuccessful():
            failed = True

    grand = sum(total for _, total, _ in rows if total)
    print("\n" + "-" * 60, file=stream)
    parts = []
    for label, total, result in rows:
        if result is None:
            parts.append(f"{label} {'目录缺失' if total is None else '0 项'}")
        elif result.wasSuccessful():
            parts.append(f"{label} {total} OK")
        else:
            bad = len(result.failures) + len(result.errors)
            parts.append(f"{label} {total} FAIL({bad})")
    print("汇总：" + "；".join(parts) + f"  | 合计 {grand} 项", file=stream)
    print("结果：" + ("全部通过" if not failed else "有失败，见上面各处输出"), file=stream, flush=True)
    return 0 if not failed else 1


def main(argv: list[str]) -> int:
    # Windows 下 stdout 默认是 GBK，重定向到文件就会变成乱码，汇总行反而看不清。
    # 统一成 UTF-8：控制台本身走 UTF-16 写入接口，不受影响。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    verbosity = 2 if ("-v" in argv or "--verbose" in argv) else 1
    return run_suites(ROOT, SUITES, verbosity=verbosity)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
