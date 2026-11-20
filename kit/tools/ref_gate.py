"""引用与锚点闸门：指向不存在的东西，当场失败。

为什么要有这个脚本：文档里的「`docs/protocol.md:135`」和「见 `AGENTS.md` 的
「分支与合并」」这两种写法，是这个工作区里最常用也最容易烂掉的引用。文件改名、
章节重写、行号漂移都不会报错，只有人顺着点过去才会发现它指不到东西。所以把
它做成退出码：过了是 0，指不到是 1，并指出引用处的位置。

核两类引用：

1. 行号引用：带目录的相对路径加行号，例如 `task_center/core.py:247`。判据是
   目标文件在、行数不少于引用行号、该行去掉空白后非空。裸文件名（不含目录
   分隔符）与绝对路径不在仓库里定位得到同一份东西，所以跳过并计数，不判对错。
2. 锚点引用：「见 某文件 的「某标题」」。判据是目标文件在，且有以 `#` 开头的
   标题，其文本等于被引标题。第 ② 类默认不全仓核：中文里「的「…」」既可能是
   跨文件标题引用，也可能只是引号里的一句话，全仓扫会把闸门变成噪声源。

用法：

    python tools/ref_gate.py --paths docs AGENTS.md PRD tasks README.md --kinds line
    python tools/ref_gate.py --paths docs/protocol.md --kinds all
    python tools/ref_gate.py --paths tests/fixtures/ref-gate --kinds line --root .

    --paths   文件或目录；目录递归收 .md。给多个用空格分隔。
    --kinds   收 line / anchor / all，也接受 line,anchor；默认 line。
    --root    解析基准目录，默认仓库根（本脚本所在目录的上一级）。

退出码：全部成立 0，有引用不成立 1，用法错误 2。末行打印被跳过的裸文件名与
绝对路径计数，好让人知道「通过」是核了多少、跳了多少之后的结果。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KINDS = ("line", "anchor")
DEFAULT_PATHS = ("docs", "AGENTS.md", "PRD", "tasks", "README.md")

# 行号引用：`路径.ext:行号`。扩展名至少一个字符，路径允许 Windows 与 POSIX 分隔符。
LINE_REF = re.compile(r"([A-Za-z0-9_./\\-]+\.[A-Za-z0-9]+):(\d+)")
# 锚点引用：见 `<路径>.md` 的「<标题>」。反引号可有可无，书名号也允许 “ ”。
ANCHOR_REF = re.compile(r"见\s*`?([A-Za-z0-9_./\\-]+\.md)`?\s*的[「“]([^」”]+)[」”]")
DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def classify(ref: str) -> str:
    """把引用目标分成 absolute / bare / relative 三类。

    绝对路径不该在仓库里核（它指向机器上的别处），裸文件名离开上下文就定位不到，
    两者都跳过。只有带目录分隔符的相对路径能被无歧义地解析。
    """
    if DRIVE.match(ref) or ref.startswith("/") or ref.startswith("\\"):
        return "absolute"
    if "/" in ref or "\\" in ref:
        return "relative"
    return "bare"


def display(path: Path, root: Path) -> str:
    """引用处文件怎么出现在报错里：能相对仓库根就相对，否则用原样。"""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def collect(paths: list[Path]) -> list[Path]:
    """展开 --paths：文件原样保留，目录递归收 .md，结果排序去重。"""
    files: list[Path] = []
    for entry in paths:
        if entry.is_dir():
            files.extend(sorted(p for p in entry.rglob("*.md") if p.is_file()))
        elif entry.is_file():
            files.append(entry)
    seen: set[Path] = set()
    unique: list[Path] = []
    for item in files:
        key = item.resolve()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def check_line_refs(source: Path, root: Path, problems: list[str], skipped: dict[str, int]) -> int:
    """核第 ① 类，返回这一文件里实际核过的引用数。"""
    checked = 0
    for lineno, line in enumerate(read_lines(source), 1):
        for match in LINE_REF.finditer(line):
            ref, number = match.group(1), int(match.group(2))
            kind = classify(ref)
            if kind != "relative":
                skipped[kind] += 1
                continue
            checked += 1
            target = root / ref
            where = f"{display(source, root)}:{lineno}"
            if not target.is_file():
                problems.append(f"{where}: 指向 {ref} 不存在")
                continue
            lines = read_lines(target)
            if number > len(lines):
                problems.append(
                    f"{where}: 指向 {ref} 的第 {number} 行超出该文件的行数（共 {len(lines)} 行）"
                )
                continue
            if not lines[number - 1].strip():
                problems.append(f"{where}: 指向 {ref} 的第 {number} 行是空行")
    return checked


def headings(path: Path) -> list[str]:
    found = []
    for line in read_lines(path):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            found.append(stripped.lstrip("#").strip())
    return found


def check_anchor_refs(source: Path, root: Path, problems: list[str], skipped: dict[str, int]) -> int:
    """核第 ② 类，返回这一文件里实际核过的引用数。"""
    checked = 0
    for lineno, line in enumerate(read_lines(source), 1):
        for match in ANCHOR_REF.finditer(line):
            ref, title = match.group(1), match.group(2).strip()
            kind = classify(ref)
            if kind != "relative":
                skipped[kind] += 1
                continue
            checked += 1
            target = root / ref
            where = f"{display(source, root)}:{lineno}"
            if not target.is_file():
                problems.append(f"{where}: 指向 {ref} 不存在")
                continue
            if title not in headings(target):
                problems.append(f"{where}: 指向 {ref} 的标题「{title}」不存在（该文件里没有以 # 开头的同名标题）")
    return checked


def parse_kinds(values: list[str]) -> set[str]:
    picked: set[str] = set()
    for chunk in values:
        for piece in chunk.replace(",", " ").split():
            if piece == "all":
                picked.update(KINDS)
            elif piece in KINDS:
                picked.add(piece)
            else:
                raise ValueError(piece)
    return picked or {"line"}


def parse_args(argv: list[str]) -> dict:
    """手写一个窄参数解析：这个脚本只有四个选项，不值得引入依赖。"""
    options: dict = {"paths": None, "kinds": ["line"], "root": ROOT}
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("-h", "--help"):
            options["help"] = True
            index += 1
        elif arg in ("--paths", "--kinds", "--root"):
            values = []
            index += 1
            while index < len(argv) and not argv[index].startswith("--"):
                values.append(argv[index])
                index += 1
            if not values:
                raise ValueError(f"{arg} 需要一个值")
            if arg == "--paths":
                options["paths"] = values
            elif arg == "--kinds":
                options["kinds"] = values
            else:
                options["root"] = Path(values[0])
        else:
            raise ValueError(f"不认识的参数 {arg}")
    return options


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        options = parse_args(argv)
    except ValueError as error:
        print(f"用法错误：{error}", file=sys.stderr)
        print(__doc__.strip(), file=sys.stderr)
        return 2
    if options.get("help") or not argv:
        print(__doc__.strip())
        return 0

    try:
        kinds = parse_kinds(options["kinds"])
    except ValueError as error:
        print(f"用法错误：{error} 不是合法的 --kinds 值（只收 line / anchor / all）", file=sys.stderr)
        return 2

    root: Path = options["root"]
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()
    if not root.is_dir():
        print(f"用法错误：--root 指向的目录不存在：{root}", file=sys.stderr)
        return 2

    raw_paths = options["paths"] or list(DEFAULT_PATHS)
    targets: list[Path] = []
    for entry in raw_paths:
        candidate = Path(entry)
        if not candidate.is_absolute():
            rooted = root / candidate
            candidate = rooted if rooted.exists() else candidate
        if not candidate.exists():
            print(f"用法错误：--paths 里这一项不存在：{entry}", file=sys.stderr)
            return 2
        targets.append(candidate)

    problems: list[str] = []
    skipped = {"bare": 0, "absolute": 0}
    checked = 0
    for source in collect(targets):
        if "line" in kinds:
            checked += check_line_refs(source, root, problems, skipped)
        if "anchor" in kinds:
            checked += check_anchor_refs(source, root, problems, skipped)
    skip_line = f"跳过：裸文件名 {skipped['bare']} 处、绝对路径 {skipped['absolute']} 处"

    if problems:
        print(f"引用闸门未通过：{len(problems)} 处引用指向不存在的东西")
        for problem in problems:
            print(" - " + problem)
        print(skip_line)
        return 1

    print(f"引用闸门通过：核了 {checked} 处引用")
    print(skip_line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
