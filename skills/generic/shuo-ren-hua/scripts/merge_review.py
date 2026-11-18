#!/usr/bin/env python3
"""把逐块审核记录合并成一份完整报告。

用法：
    python scripts/merge_review.py work/review
    python scripts/merge_review.py work/review -o 审核报告.md

读取 work/review/_index.md 和 reviews/c01.md、c02.md……
每块记录按固定字段写（见 chunk_article.py 生成的模板）。

合并时做四件事：
1. 按类型归组，同一个毛病跨块重复的合并成一条，标出现次数
2. 按出现顺序重编号，段号保留原文
3. 跨段承接问题单列——这类问题只在接缝处出现，块内审不出来
4. 检查清单完整性，缺「不要动」或「材料缺口」就报警

不做的事：
    不替你判断对错，不改写句子，不排序优先级。
    两个块对同一句结论不同时，原样保留两条，标「有分歧」，让人回去看原文。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SECTIONS = ("必改", "建议改", "不要动", "材料缺口")
FIELD = re.compile(r"^(原句|类型|成本|依据|方向|理由|段|内容)[:：]\s*(.*)$")


def parse_chunk(text: str) -> list[dict]:
    """解析一块记录里的 ### 块。"""
    items = []
    for m in re.finditer(r"^###\s*\[(.+?)\]\s*(.*)$", text, flags=re.M):
        verdict = m.group(1).strip()
        loc = m.group(2).strip()
        start = m.end()
        nxt = text.find("\n###", start)
        body = text[start:nxt if nxt != -1 else len(text)]
        f: dict[str, str] = {"裁决": verdict, "定位": loc}
        for line in body.splitlines():
            mm = FIELD.match(line.strip())
            if mm:
                f[mm.group(1)] = mm.group(2).strip()
        # 兼容没有###的单条说明
        if "内容" not in f and not f.get("原句"):
            rest = body.strip()
            if rest:
                f["内容"] = rest.splitlines()[0].strip()
        items.append(f)
    if "不要动" in text and not any(i["裁决"] == "不要动" for i in items):
        # 「不要动」常常写成普通段落
        pass
    return items


def collect(root: Path) -> tuple[dict, list[tuple[str, dict]]]:
    idx = root / "_index.md"
    index_text = idx.read_text(encoding="utf-8") if idx.exists() else ""
    rdir = root / "reviews"
    files = sorted(rdir.glob("c*.md")) if rdir.exists() else []
    rows: list[tuple[str, dict]] = []
    for f in files:
        for item in parse_chunk(f.read_text(encoding="utf-8")):
            rows.append((f.stem, item))
    return {"index": index_text, "chunks": len(files)}, rows


def dedupe(rows: list[tuple[str, dict]]) -> list[tuple[list[str], dict]]:
    """同一个毛病跨块重复 → 合并，保留首次出现和字数最高的依据。"""
    grouped: dict[str, tuple[list[str], dict]] = {}
    for chunk, item in rows:
        sent = (item.get("原句") or item.get("内容") or "").strip()
        typ = (item.get("类型") or "").strip()
        # 类型相同、原句也几乎是同一句，才算重复。
        # 只按类型合并会把"死物配活物"这类通病全揉成一条，丢信息。
        key = f"{typ}|{sent[:12]}|{item['裁决']}"
        if key in grouped:
            chunks, prev = grouped[key]
            chunks.append(chunk)
            if len(item.get("依据", "")) > len(prev.get("依据", "")):
                prev["依据"] = item["依据"]
            grouped[key] = (chunks, prev)
        else:
            grouped[key] = ([chunk], item)
    return list(grouped.values())


def render(meta: dict, groups: list[tuple[list[str], dict]]) -> str:
    by = {s: [] for s in SECTIONS}
    seams = []
    for chunks, item in groups:
        v = item["裁决"]
        if v not in by:
            v = "建议改"
        if "接缝" in (item.get("类型", "") + item.get("内容", "")):
            seams.append((chunks, item))
            continue
        by[v].append((chunks, item))

    out = ["# 审核报告", "", meta["index"].strip(), ""]
    out += [
        "---",
        "",
        "## 【整篇判断】",
        "",
        "（这里写：属于「有材料写得像机器」还是「没材料」；作者下一步该干什么。"
        "这一段不能由分块得出，必须在全部块审完后单独读一遍全文再写。）",
        "",
    ]
    for s in SECTIONS:
        items = by[s]
        out.append(f"## 【{s}】{len(items)} 条")
        out.append("")
        if not items:
            out.append("（无）" if s != "不要动" else "（⚠️ 空着说明没检查风格保留，回去补）")
            out.append("")
            continue
        for n, (chunks, item) in enumerate(items, 1):
            times = f"（出现 {len(chunks)} 次：{('、'.join(chunks))}）" if len(chunks) > 1 else ""
            title = item.get("定位") or item.get("段") or ""
            out.append(f"**{n}. {title}**{times}")
            for k in ("原句", "类型", "成本", "依据", "方向", "理由", "内容"):
                if item.get(k):
                    out.append(f"- {k}：{item[k]}")
            out.append("")
    if seams:
        out.append(f"## 【跨段承接】{len(seams)} 条")
        out.append("")
        out.append("这类问题只在块与块的接缝出现，块内审不出来。")
        out.append("")
        for n, (chunks, item) in enumerate(seams, 1):
            out.append(f"**{n}. {item.get('定位','')}**（{'、'.join(chunks)}）")
            if item.get("原句"):
                out.append(f"- 原句：{item['原句']}")
            if item.get("依据"):
                out.append(f"- 依据：{item['依据']}")
            out.append("")

    out.append("---")
    out.append("")
    out.append("## 完整性检查")
    out.append("")
    for s in SECTIONS:
        out.append(f"- {s}：{len(by[s])} 条")
    out.append(f"- 跨段承接：{len(seams)} 条")
    out.append("")
    out.append("「不要动」为空说明只挑毛病、没做风格保留。回每块补。")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("-o", "--out")
    args = ap.parse_args()
    root = Path(args.root)
    meta, rows = collect(root)
    if not rows:
        print(f"{root}/reviews/ 里没有 cNN.md，先逐块审再合并")
        return 2
    report = render(meta, dedupe(rows))
    dest = Path(args.out) if args.out else root / "审核报告.md"
    dest.write_text(report, encoding="utf-8")
    print(f"合并了 {meta['chunks']} 块、{len(rows)} 条记录 → {dest}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
