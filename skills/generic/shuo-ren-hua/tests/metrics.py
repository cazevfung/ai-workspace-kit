#!/usr/bin/env python3
"""测试台：把一轮稿子拆成文章，跑体检，输出对比表。

用法：
    python tests/metrics.py tests/orig.md
    python tests/metrics.py tests/orig.md tests/r1.md tests/r2.md

只输出机器能量出来的部分。需要人判断的项（回读次数、突兀比喻、命题是否守恒）
记在 tests/report.md 里。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import lint_readability as lr  # noqa: E402

LABEL = [
    ("long", "长句"),
    ("verbless", "无谓语片段"),
    ("far_pred", "首谓远"),
    ("attribute", "长定语"),
    ("de_chain", "的链"),
    ("animate", "死物配活物"),
    ("nominal", "名词化"),
    ("enum3", "顿号三项"),
]


def split_articles(text: str) -> list[tuple[str, str]]:
    parts = re.split(r"^## ", text, flags=re.M)
    out = []
    for p in parts:
        if not p.strip():
            continue
        head, _, body = p.partition("\n")
        out.append((head.strip(), body.strip()))
    return out


def main() -> int:
    files = sys.argv[1:]
    if not files:
        print(__doc__)
        return 2

    for f in files:
        p = Path(f)
        print(f"\n## {p.name}\n")
        header = "| 篇目 | 字数 | " + " | ".join(lbl for _, lbl in LABEL) + " | 承接词/千字 |"
        print(header)
        print("|" + "---|" * (len(LABEL) + 3))
        for title, body in split_articles(p.read_text(encoding="utf-8")):
            sents = lr.split_sentences(body)
            hits = lr.check(sents)
            cells = [f"{len(hits[k])}" for k, _ in LABEL]
            conn = lr.density(body, lr.CONN)
            print(f"| {title[:14]} | {lr.chinese_len(body)} | " + " | ".join(cells) + f" | {conn:.1f} |")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
