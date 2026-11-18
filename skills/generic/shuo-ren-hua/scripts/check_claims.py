#!/usr/bin/env python3
"""命题对账：比对原文和改写稿里的"硬信息"，找出可能被删掉或被编造的部分。

用法：
    python check_claims.py 原文.md 改写.md
    python check_claims.py 原文.md 改写.md --quiet   # 只报差异

它能对账的东西：
    数字、百分比、英文词与型号、引号内的文字、专名候选、否定词、限定词

它不能对账的东西：
    观点、因果、让步关系。这些只能人看。

所以输出的是"待人工确认"，不是"错误"。
被报出来不等于改错了：同义替换、合并句子都会产生差异。
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

LEX = {
    "数字": re.compile(r"\d+(?:\.\d+)?%?|(?<![一二三四五六七八九十])(?:[二三四五六七八九十][零一二三四五六七八九十]*(?:个|次|年|月|天|人|倍|成|条|种))"),
    "英文": re.compile(r"[A-Za-z][A-Za-z0-9\.\-]*"),
    "引号": re.compile(r"[“\"「『]([^”\"」』]{1,40})[”\"」』]"),
    "否定": re.compile(r"不|没|未|无|别|非|免"),
    "限定": re.compile(r"可能|也许|或许|通常|往往|一般|大多|少数|部分|某种|某种程度|据说|大约|左右|基本"),
}


def extract(text: str) -> dict[str, Counter]:
    out = {}
    for name, pat in LEX.items():
        out[name] = Counter(m if isinstance(m, str) else m.group(0) for m in pat.findall(text))
    return out


def diff(a: Counter, b: Counter) -> tuple[list[str], list[str]]:
    lost = [k for k in a if a[k] > b.get(k, 0)]
    added = [k for k in b if b[k] > a.get(k, 0)]
    return sorted(lost), sorted(added)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("orig")
    ap.add_argument("new")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    a = extract(Path(args.orig).read_text(encoding="utf-8"))
    b = extract(Path(args.new).read_text(encoding="utf-8"))

    total_delta = 0
    for name in LEX:
        lost, added = diff(a[name], b[name])
        total_delta += len(lost) + len(added)
        if not lost and not added:
            continue
        print(f"\n### {name}")
        if lost:
            print("  原文有、改写后少了（可能删了命题）：")
            for k in lost[:20]:
                print(f"    - {k}  ({a[name][k]} → {b[name].get(k, 0)})")
        if added:
            print("  改写后多出来的（可能编造）：")
            for k in added[:20]:
                print(f"    - {k}  ({a[name].get(k, 0)} → {b[name][k]})")

    if total_delta == 0 and not args.quiet:
        print("硬信息没有差异。")

    print("\n---")
    print("以上每一项都要人看一眼：同义替换、合并句子都会产生差异。")
    print("但\"原文有、改后少\"里如果出现数字、专名、引语，基本就是删了命题，必须补回去。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
