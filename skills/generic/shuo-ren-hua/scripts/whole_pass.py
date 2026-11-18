#!/usr/bin/env python3
"""整篇复读简报。

用法：
    python scripts/whole_pass.py 稿件.md
    python scripts/whole_pass.py 稿件.md --index work/review/_index.md

为什么需要它：
    分段审完，人手里只有一堆局部结论。整篇复读要从头读到尾，
    但带着一堆条目去读，眼睛会一直在找句子细节，看不见整篇。
    这个脚本只给四样东西，逼你把注意力放在跨块的问题上。

输出四样：
    1. 权重分布：每块占全文多少字。某块 50% 而只讲一个细节，就是重心偏了。
    2. 每段首句：连起来读，看是不是罗列。
    3. 跨段相似句：不同位置的句子意思重复。这一段分段审永远看不出来。
    4. 长度节奏：每块平均句长。整篇忽长忽短，读着会累。

它不做的：判断好坏、提意见、打分。那是复读时人干的活。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def split_paras(text: str) -> list[str]:
    raw = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(raw) <= 2:
        raw = [x.strip() for x in text.splitlines() if x.strip()]
    out, glue = [], False
    for p in raw:
        if glue and out:
            out[-1] += "\n" + p
            glue = False
            continue
        out.append(p)
        glue = bool(re.match(r"^#{1,6}\s", p)) and len(p.splitlines()) == 1
    return out


def split_sents(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[。！？；!?;])", text) if len(s.strip()) >= 10]


def grams(s: str, n: int = 3) -> set[str]:
    s = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", s)
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


def overlap(a: str, b: str) -> float:
    """包含度，不是杰卡德。短句套在长句里时杰卡德会严重低估。"""
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--index", default="", help="分块索引，传了就按块算权重")
    ap.add_argument("--sim", type=float, default=0.6)
    args = ap.parse_args()

    text = Path(args.path).read_text(encoding="utf-8")
    paras = split_paras(text)
    sents = split_sents(text)
    total = sum(len(re.sub(r"\s", "", p)) for p in paras) or 1

    print("# 整篇复读简报\n")
    print(f"段落 {len(paras)}，句 {len(sents)}，总字数 {total}\n")

    print("## 一、权重分布\n")
    print("| 段 | 字数 | 占比 | 首句 |")
    print("|---|---|---|---|")
    for i, p in enumerate(paras, 1):
        n = len(re.sub(r"\s", "", p))
        head = split_sents(p)[0][:26] if split_sents(p) else p[:26]
        flag = " ⚠️" if n / total > 0.35 and len(paras) > 3 else ""
        print(f"| {i} | {n} | {n / total:.0%}{flag} | {head} |")
    print()

    print("## 二、每段首句（连起来读，能换顺序就是罗列）\n")
    heads = []
    for i, p in enumerate(paras, 1):
        ss = split_sents(p)
        if ss:
            heads.append(f"{i}. {ss[0]}")
    print("\n".join(heads))
    print()

    print("## 三、跨段相似句（意思重复，分段审看不出来）\n")
    found = 0
    cand = [s for s in sents if not s.startswith("#") and len(s) >= 14]
    for i in range(len(cand)):
        for j in range(i + 2, len(cand)):
            score = overlap(cand[i], cand[j])
            if score >= args.sim:
                found += 1
                print(f"- {score:.2f}｜{cand[i][:34]} … ⟷ … {cand[j][:34]}")
                if found >= 12:
                    break
        if found >= 12:
            break
    if not found:
        print("没找到字面高度重复的句子。")
        print("注意：这一步只抓字面重复。意思重复（换了个说法说同一件事）抓不到，得人读。")
    print()

    print("## 四、句长节奏\n")
    lens = [len(re.sub(r"\s", "", s)) for s in sents]
    if lens:
        avg = sum(lens) / len(lens)
        print(f"平均 {avg:.0f} 字，最长 {max(lens)}，最短 {min(lens)}")
        short = sum(1 for x in lens if x <= 8)
        print(f"8 字以内的短句 {short} 句（占 {short / len(lens):.0%}）")
        print("\n忽长忽短是正常的。整篇句长都在 15–25 之间，读着会平。")
    print()

    print("---\n")
    print("## 复读时只找这六样\n")
    for x in (
        "1. 跨块重复：第 2 段和第 6 段是不是说了同一件事",
        "2. 推进断裂：接缝都顺，但三块都在举例，没有一次往前走",
        "3. 权重失衡：整块 200 字讲一个细节，核心结论只有一句",
        "4. 可删整段：删掉这一整段，读者少什么？什么都不少就该删",
        "5. 语气漂移：开头像说话，中间变成公文",
        "6. 首尾兑现：开头答应给的东西，结尾给了吗",
    ):
        print(x)
    print("\n句子细节各块已经审过了。复读时不要回去抠句子，那是第四遍。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
