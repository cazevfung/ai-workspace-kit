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
  `skills/<名>/` 里的脚本测试同理，属于技能自己的交付线，不进主线 suite
  （2026-11-21，AW-081：`skills/ui-design/scripts/test_check_ui.py` 要单独跑）。

两处用例互不依赖、也不共用临时库与临时目录，却挤在同一个进程里串行跑，
一次全量要 140 秒上下，而 auto_merge 一次合并要跑两遍（AW-110）。现在默认
**两处各起一个子进程同时跑**，输出按块归属：一处跑完的内容整块打完，再打另
一处，不会你一句我一句。做法是子进程各自写一个临时文件，父进程读完再整块
打印——不直接把 stdout 透传过来，因为 Windows 的管道下两个进程交错写，
缓冲与编码一交叉，行就会混在一起。

并行不改退出码语义：任一处红、目录缺失、发现 0 项、有目录未声明，整体都是 1；
汇总行仍然分别打两处的用例数、合计，并额外列出「红在哪一处、哪几条用例」。
排查单处问题时用 `--serial` 退回同进程串行（老路径保留为 `run_suites`）。

用法（在仓库根 {{WORKSPACE}}）：

    python tools/run_tests.py           # 默认：两处并行
    python tools/run_tests.py -v        # 逐个用例名
    python tools/run_tests.py --serial  # 串行：排查时用，看输出先后关系

退出码：全部通过 0；任一处有失败/错误、目录缺失、发现 0 项、或有未声明的测试目录，都是 1。
"""
from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys
import tempfile
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
# `skills/` 同理（2026-11-21，AW-081）：仓库里的 `skills/<名>/` 是技能源码，技能随身的
# 脚本用例（例如 `skills/ui-design/scripts/test_check_ui.py`）按 PRD-011 §6 不进主线套件，
# 要单独跑自己的命令。不跳过它，这条自检会把「按规范放的用例」报成漏跑。
SKIP_DIRS = frozenset(
    {".git", ".worktrees", "_sandbox", "__pycache__", ".data", ".claude", ".agents", "skills"}
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


def report_undeclared(root: Path, suites, stream) -> bool:
    """自检：有 test_*.py 却没进 SUITES 的目录。返回 True 表示有失败。"""
    declared = [Path(path).relative_to(root).as_posix() for _, path, _ in suites]
    undeclared = check_undeclared_suites(root, declared)
    if not undeclared:
        return False
    print("\n=== 自检：SUITES 覆盖 ===", file=stream, flush=True)
    print("失败：下面这些目录里有 test_*.py，却不在 SUITES 里，其中的用例永远不会被跑到：", file=stream)
    for name in undeclared:
        print(f"  - {name}/", file=stream)
    print("新增测试目录请加进 SUITES（本文件顶部的 SUITES 常量），否则等于没写测试。", file=stream, flush=True)
    return True


def run_one_suite(label: str, path, pattern: str, verbosity: int = 1, stream=None) -> dict:
    """跑一处用例，返回这一处的结论：total / ok / bad / failed_ids。

    目录缺失与「发现 0 项」都算这一处失败，理由见文件顶部（AW-016）。
    串行与并行共用这一份：并行时它跑在子进程里，输出进子进程自己的临时文件。
    """
    stream = stream if stream is not None else sys.stdout
    path = Path(path)
    if not path.is_dir():
        print(f"\n=== {label}（目录缺失）===", file=stream, flush=True)
        print(f"失败：{label} 声明的测试目录不存在：{path}", file=stream)
        print("这一处一个用例都没跑到，不能当作通过。", file=stream, flush=True)
        return {"total": None, "ok": False, "bad": 0, "failed_ids": []}

    suite = discover(path, pattern)
    total = suite.countTestCases()
    print(f"\n=== {label} ({total} 项) ===", file=stream, flush=True)
    if total == 0:
        print(f"失败：{label} 发现 0 项用例（目录在，但没有任何匹配 {pattern} 的文件）。", file=stream, flush=True)
        return {"total": 0, "ok": False, "bad": 0, "failed_ids": []}

    result = unittest.TextTestRunner(stream=stream, verbosity=verbosity).run(suite)
    bad_cases = list(result.failures) + list(result.errors)
    return {
        "total": total,
        "ok": result.wasSuccessful(),
        "bad": len(bad_cases),
        "failed_ids": [case.id() for case, _ in bad_cases],
    }


def summarize(rows, stream, preflight_failed: bool = False) -> int:
    """打汇总：每处的用例数与结论、合计、以及红在哪几条用例上。返回退出码。"""
    grand = sum(total for _, total, _ in rows if total)
    failed = preflight_failed
    print("\n" + "-" * 60, file=stream)
    parts = []
    for label, total, info in rows:
        if info["ok"]:
            parts.append(f"{label} {total} OK")
            continue
        failed = True
        if info["bad"]:
            parts.append(f"{label} {total} FAIL({info['bad']})")
        else:
            parts.append(f"{label} {'目录缺失' if total is None else '0 项'}")
    print("汇总：" + "；".join(parts) + f"  | 合计 {grand} 项", file=stream)
    # 一处红要让「红在哪一处、哪一条用例」在汇总里就能看到，不必往回翻 traceback。
    for label, total, info in rows:
        if info["failed_ids"]:
            print(f"红在 {label}：" + "、".join(info["failed_ids"]), file=stream)
    print("结果：" + ("全部通过" if not failed else "有失败，见上面各处输出"), file=stream, flush=True)
    return 0 if not failed else 1


def run_suites(root: Path, suites, verbosity: int = 1, stream=None) -> int:
    """串行跑完 suites 里的每一处（`--serial` 走这条），返回 0 表示全通过。

    root 与 suites 都是参数（不是直接读全局），这样测试能拿临时目录跑真实流程。
    """
    stream = stream if stream is not None else sys.stdout
    root = Path(root)
    preflight_failed = report_undeclared(root, suites, stream)
    rows = []
    for label, path, pattern in suites:
        info = run_one_suite(label, path, pattern, verbosity=verbosity, stream=stream)
        rows.append((label, info["total"], info))
    return summarize(rows, stream, preflight_failed=preflight_failed)


# 子进程入口的开关：并行时父进程用它把「只跑一处」的活派给子进程。
CHILD_FLAG = "--one-suite"


def _child_argv(root, label, path, pattern, verbosity: int, report_path) -> list[str]:
    """拼出「跑一处并留下结论文件」的子进程命令行。"""
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        CHILD_FLAG,
        "--root", str(Path(root).resolve()),
        "--label", label,
        "--path", str(Path(path).resolve()),
        "--pattern", pattern,
        "--verbosity", str(verbosity),
        "--report", str(report_path),
    ]


def _child_main(argv: list[str]) -> int:
    """子进程入口：只跑一处。stdout 由父进程重定向到临时文件，结论写进 --report。"""
    args = [item for item in argv if item != CHILD_FLAG]
    opts = dict(zip(args[0::2], args[1::2]))
    root = Path(opts["--root"])
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    info = run_one_suite(
        opts["--label"],
        Path(opts["--path"]),
        opts["--pattern"],
        verbosity=int(opts["--verbosity"]),
        stream=sys.stdout,
    )
    Path(opts["--report"]).write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return 0 if info["ok"] else 1


def run_suites_parallel(root: Path, suites, verbosity: int = 1, stream=None) -> int:
    """每处用例各起一个子进程同时跑，输出按块归属；返回 0 表示全通过。

    为什么不直接把子进程的 stdout 接过来：Windows 下两个进程往同一条管道写，
    缓冲与编码一交叉，两处的行就会你一句我一句——而「不交错」正是这张单要的。
    所以每个子进程各自写一个临时文件（stdout 与 stderr 都进那个文件），全跑完
    父进程再按 SUITES 的顺序整块打出来。结论另用一个 JSON 文件传回来，不靠解析
    人看的输出，免得换一句措辞就把退出码语义弄坏。
    """
    stream = stream if stream is not None else sys.stdout
    root = Path(root)
    print(
        f"\n并行模式：{len(suites)} 处用例各起一个子进程同时跑（--serial 可退回串行）",
        file=stream,
        flush=True,
    )
    preflight_failed = report_undeclared(root, suites, stream)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    rows = []
    with tempfile.TemporaryDirectory(prefix="run-tests-") as tmpdir:
        tmp = Path(tmpdir)
        jobs = []
        for index, (label, path, pattern) in enumerate(suites):
            out_path = tmp / f"{index}.out"
            report_path = tmp / f"{index}.report.json"
            handle = open(out_path, "wb")
            proc = subprocess.Popen(
                _child_argv(root, label, path, pattern, verbosity, report_path),
                stdout=handle,
                stderr=subprocess.STDOUT,
                cwd=str(root),
                env=env,
            )
            jobs.append((label, out_path, report_path, handle, proc))

        for _, _, _, handle, proc in jobs:
            proc.wait()
            handle.close()

        for label, out_path, report_path, _, proc in jobs:
            text = ""
            if out_path.is_file():
                text = out_path.read_text(encoding="utf-8", errors="replace")
            if text:
                print(text, end="" if text.endswith("\n") else "\n", file=stream, flush=True)
            else:
                print(f"\n=== {label}（子进程没有输出）===", file=stream, flush=True)

            info = None
            if report_path.is_file():
                try:
                    info = json.loads(report_path.read_text(encoding="utf-8"))
                except ValueError:
                    info = None
            if info is None:
                print(
                    f"失败：{label} 的子进程退出码 {proc.returncode}，没留下结论文件（可能中途崩了）。",
                    file=stream,
                    flush=True,
                )
                info = {"total": None, "ok": False, "bad": 0, "failed_ids": []}
            rows.append((label, info["total"], info))

    return summarize(rows, stream, preflight_failed=preflight_failed)


def main(argv: list[str], root=None, suites=None, stream=None) -> int:
    """默认两处并行；--serial 退回同进程串行（排查单处问题时用）。

    root / suites / stream 可注入，测试能拿临时目录跑真实流程。
    """
    if CHILD_FLAG in argv:
        return _child_main(argv)
    root = ROOT if root is None else Path(root)
    suites = SUITES if suites is None else suites
    if stream is None:
        # Windows 下 stdout 默认是 GBK，重定向到文件就会变成乱码，汇总行反而看不清。
        # 统一成 UTF-8：控制台本身走 UTF-16 写入接口，不受影响。
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
        stream = sys.stdout
    verbosity = 2 if ("-v" in argv or "--verbose" in argv) else 1
    if "--serial" in argv:
        return run_suites(root, suites, verbosity=verbosity, stream=stream)
    return run_suites_parallel(root, suites, verbosity=verbosity, stream=stream)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
