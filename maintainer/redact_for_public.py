#!/usr/bin/env python3
"""发布前脱敏：把同步带进来的本机信息换成占位值。

`sync_from_origin.py` 只处理路径模板化，而且 `tests/fixtures` 是有意**原样复制**的
（夹具里的绝对路径是判据的一部分，不能被 `{{WORKSPACE}}` 替换掉）。所以每次同步之后，
真实用户名与真实会话 id 会跟着进来。这个脚本在同步之后跑一次，把它们清掉。

规则（只对文本文件生效，`maintainer/` 自身除外——那里写着怎么匹配本机路径）：

    C:\\Users\\<名>\\ 或 X:/示例项目/ 或 X:/示例项目   → X:/示例项目
    8-4-4-4-12 形状的 UUID                               → <session-id>

用法：
    python maintainer/sync_from_origin.py
    python maintainer/redact_for_public.py           # 打印改了哪些文件、多少处
    python maintainer/redact_for_public.py --check   # 只报不改，有残留则退出 1

为什么 UUID 也统一替换：套件里出现的 UUID 全部是真实会话 / 尝试 id，没有一处靠它的
形状做断言（`tests/test_notice.py` 的 ghost 夹具只要求「查不到」，`session_roster.py`
的用法示例同理）。换成 `<session-id>` 不影响任何验证逻辑。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".jsonl", ".sh", ".ps1", ".toml", ".cfg", ".ini", ".yml", ".yaml"}
SKIP_DIRS = {".git", ".data", "__pycache__", ".worktrees", "node_modules", "maintainer"}

UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
HOME_ROOT = re.compile(r"(?:C:[\\/]Users[\\/][^\\/\s`\"']+|%USERPROFILE%)")
# 替换根之后，把该路径段里剩下的反斜杠归一成斜杠，读起来一致
EXAMPLE_PATH = re.compile(r"X:/示例项目[^\s`\"')|，；、]*")
# 示例根下面再垫一层业务目录（`Documents/<项目>`）没有信息量，压平成 `X:/示例项目/<项目>`
EXAMPLE_NESTED = re.compile(r"X:/示例项目/Documents/[^/\s`\"']+/")


def _normalize(match: re.Match[str]) -> str:
    return match.group(0).replace("\\", "/")

# 同步带进来的上游业务专名。逐条登记，每发现一处加一条，下次同步才会自动清掉。
UPSTREAM_TERMS = (
    ("这份表核的是 1205 版本的结案材料", "这份表核的是一份结案材料"),
)


def redact(text: str) -> str:
    text = HOME_ROOT.sub("X:/示例项目", text)
    text = EXAMPLE_PATH.sub(_normalize, text)
    text = EXAMPLE_NESTED.sub("X:/示例项目/", text)
    text = UUID.sub("<session-id>", text)
    for raw, clean in UPSTREAM_TERMS:
        text = text.replace(raw, clean)
    return text


def iter_text_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        yield path


BASELINE_REL = "kit/tools/evidence_baseline.txt"
CLEAN_BASELINE = """# 历史计划豁免清单（证据可判定判据）
#
# 为什么有这份文件：`tools/doc_gate.py` 里有「完成证据必须可判定」这条判据。
# 判据生效前写的计划是当时规矩的产物，回头拿新尺子量历史等于改写历史，
# 所以这里登记豁免，而不是去给旧计划补命令。
#
# 豁免范围：只豁免「完成证据不可判定」这一条判据。六节、依据、skill、交付物
# 这些既有检查对它们照跑（基线里任何一份计划少一节，`--all` 依旧非零）。
#
# 格式：每行 `tasks/<计划文件名>`，`#` 之后写理由；`#` 开头的整行是注释。
#
# 本套件不携带原工作区的历史计划，所以基线为空；自己的历史计划按格式往下加。
"""


def strip_origin_baseline() -> bool:
    """把豁免清单换成套件自己的空基线。

    原工作区的 `tasks/plan-00N-*.md` 不会随套件分发，留着那些条目只会让新用户
    去找不存在的文件。换成固定内容也保证每次同步后结果一样。
    """
    path = HERE / BASELINE_REL
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    if text == CLEAN_BASELINE:
        return False
    path.write_text(CLEAN_BASELINE, encoding="utf-8")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="发布前脱敏")
    parser.add_argument("--check", action="store_true", help="只报残留，不写文件")
    args = parser.parse_args(argv)

    changed = []
    for path in iter_text_files(HERE):
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        fixed = redact(text)
        if fixed == text:
            continue
        hits = sum(1 for a, b in zip(text.splitlines(), fixed.splitlines()) if a != b)
        changed.append((path.relative_to(HERE).as_posix(), hits))
        if not args.check:
            path.write_bytes(fixed.encode("utf-8"))

    if not args.check and strip_origin_baseline():
        changed.append((BASELINE_REL, 1))

    if not changed:
        print("脱敏检查：无残留")
        return 0
    for name, hits in changed:
        print(f"{'残留' if args.check else '已清理'}  {name}  ({hits} 行)")
    print(f"\n共 {len(changed)} 个文件。")
    return 1 if args.check else 0


if __name__ == "__main__":
    raise SystemExit(main())
