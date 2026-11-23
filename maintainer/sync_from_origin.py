#!/usr/bin/env python3
"""从已验证的原工作区同步框架文件，生成可分发的套件源码。

用途：套件里 `kit/` 与 `skills/` 的大部分文件来自一个已经在跑的原工作区。
人工复制容易漏、容易把本机绝对路径带进来。这个脚本把「同步什么、怎么改」写成代码，
运行一次就能重建，改完再抽查即可。

它不会改动原工作区，只读原目录、写套件目录。原工作区的目录不写死在代码里：
用 `--origin` 给，或者先用环境变量 `AI_WS_ORIGIN` 指定；两者都没有时脚本直接退出，
不猜一个默认目录——猜出来的默认值只在那台机器上成立，而这段代码是公开的。

用法：
    python maintainer/sync_from_origin.py --origin <原工作区目录>
    AI_WS_ORIGIN=<原工作区目录> python maintainer/sync_from_origin.py

替换规则（只对文本文件生效）：
    原工作区目录的三种写法（正斜杠、单反斜杠、转义后的双反斜杠）  →  {{WORKSPACE}}
    C:/Users/<名>  C:\\Users\\<名>                        →  %USERPROFILE%

用户名不写死：`C:/Users/<名字>` 这种本机账号只属于原工作区，不该进公开仓库，
所以按「`C:` + 分隔符 + `Users` + 分隔符 + 名字」的通用形状匹配，名字取到下一个
路径分隔符、空白或引号为止。

`{{WORKSPACE}}` 由 install.py 在安装时替换成真实的绝对路径。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".jsonl", ".sh", ".ps1", ".toml", ".cfg", ".ini", ".yml", ".yaml"}


# 用户目录：`C:\\Users\\x`（两个反斜杠）、`C:\Users\x`（一个）与 `C:/Users/x` 三种写法
# 都要认。名字止于下一个分隔符、空白或引号——用户名允许中文、空格之外的点与横线。
USER_HOME = re.compile(r"C:(?:\\\\|\\|/)Users(?:\\\\|\\|/)[^\\/\s`\"']+")


def path_variants(origin: str) -> tuple[str, ...]:
    """原工作区路径的三种写法：双反斜杠（源码里的转义写法）、单反斜杠、正斜杠。

    这三种写法由实参派生，而不是写成常量：同一份代码对任何原目录都成立，
    公开仓库里也就没有「某台机器的路径」需要跟着改。顺序按「先换更长的」排，
    否则单反斜杠那一遍会把双反斜杠那种写法拆成两半、留下孤立的单个反斜杠。
    """
    posix = str(origin).replace("\\", "/").rstrip("/")
    return (posix.replace("/", "\\\\"), posix.replace("/", "\\"), posix)


def replace_paths(text: str, origin: str) -> str:
    for form in path_variants(origin):
        text = text.replace(form, "{{WORKSPACE}}")
    return USER_HOME.sub("%USERPROFILE%", text)


def copy_text(src: Path, dst: Path, origin: str) -> bool:
    raw = src.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(replace_paths(text, origin).encode("utf-8"))
    return True


def copy_file(src: Path, dst: Path, origin: str) -> None:
    if src.suffix.lower() in TEXT_SUFFIXES:
        if copy_text(src, dst, origin):
            return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def copy_tree(src: Path, dst: Path, origin: str, skip=(), only_suffix=(), raw_parts=()) -> int:
    count = 0
    if not src.is_dir():
        print(f"  ! 源目录不存在，跳过：{src}")
        return 0
    for path in sorted(src.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(src)
        parts = rel.parts
        if "__pycache__" in parts or path.suffix == ".pyc":
            continue
        if any(part in skip for part in parts):
            continue
        if only_suffix and path.suffix.lower() not in only_suffix:
            continue
        if any(part in raw_parts for part in parts):
            # 测试夹具里的绝对路径是判据的一部分，不能被模板化。
            dst_path = dst / rel
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dst_path)
        else:
            copy_file(path, dst / rel, origin)
        count += 1
    return count


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="从原工作区同步套件源码")
    parser.add_argument("--origin", default=os.environ.get("AI_WS_ORIGIN"),
                        help="原工作区目录；也可用环境变量 AI_WS_ORIGIN 给。不猜默认值")
    args = parser.parse_args(argv)
    if not args.origin:
        print("缺 --origin：这个脚本不猜默认目录。\n"
              "用法：python maintainer/sync_from_origin.py --origin <原工作区目录>",
              file=sys.stderr)
        return 2
    origin = Path(args.origin)
    origin_text = args.origin
    if not (origin / "task_center" / "core.py").is_file():
        print(f"原工作区不完整或不正确：{origin}", file=sys.stderr)
        return 2

    kit = HERE / "kit"
    skills = HERE / "skills"
    design = HERE / "design"

    print("同步框架代码 → kit/")
    for name in ("task_center", "tools", "tests", "examples"):
        n = copy_tree(origin / name, kit / name, origin_text, skip={".worktrees", ".data"},
                      raw_parts={"fixtures"})
        print(f"  {name}: {n} 个文件")

    print("同步框架文档 → kit/docs/")
    n = copy_tree(origin / "docs", kit / "docs", origin_text, only_suffix={".md"})
    print(f"  docs: {n} 个文件")

    print("同步提交钩子 → kit/.githooks/")
    n = copy_tree(origin / ".githooks", kit / ".githooks", origin_text)
    print(f"  .githooks: {n} 个文件")

    print("同步通用技能 → skills/generic/")
    n = copy_tree(origin / "skills", skills / "generic", origin_text, skip={".claude", "__pycache__"})
    print(f"  generic: {n} 个文件")

    print("同步工作区技能 → skills/workspace/")
    n = copy_tree(origin / ".agents" / "skills", skills / "workspace", origin_text, skip={"__pycache__"})
    print(f"  workspace: {n} 个文件")

    print("同步设计史 → design/")
    for name in ("README.md", "AGENTS.md"):
        src = origin / name
        if src.is_file():
            copy_file(src, design / f"{name}.origin", origin_text)
    prd = origin / "PRD"
    if prd.is_dir():
        for item in sorted(prd.glob("PRD-*.md")):
            copy_file(item, design / "PRD" / item.name, origin_text)
    n = len(list((design / "PRD").glob("*.md"))) if (design / "PRD").is_dir() else 0
    print(f"  PRD: {n} 个文件")

    print("\n完成。接下来由人工维护的文件：README、install.py、kit/AGENTS.md、kit/PRD 模板等。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
