#!/usr/bin/env python3
"""把长文切成带上下文的审核块。

用法：
    python scripts/chunk_article.py 稿件.md --size 500 --max-paras 5
    python scripts/chunk_article.py 稿件.md -o work/review

产出：
    work/review/_index.md     全局信息 + 块清单（先读这个）
    work/review/c01.md ...    每块：前段末句 + 本块正文 + 后段首句

为什么要带前后一句：
    判断"承接"和"指代"必须看到接缝。只给本块，AI 会把本来接得上的地方判成断了。

为什么按段不按字数硬切：
    列表、表格、引用块切开就废了。段落是天然的判断单位。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def split_blocks(text: str) -> list[str]:
    """按空行切段，标题和紧随其后的正文绑在一起。

    没有空行分段的稿子（纯文本、从网页扒的），退一步按小标题切。
    """
    raw = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(raw) <= 2 and chinese_len(text) > 800 and re.search(r"^#{2,3}\s", text, re.M):
        raw = []
        for seg in re.split(r"^(?=#{2,3}\s)", text, flags=re.M):
            lines = [x.strip() for x in seg.splitlines() if x.strip()]
            buf: list[str] = []
            for line in lines:
                buf.append(line)
                if chinese_len("".join(buf)) > 260:
                    raw.append("\n".join(buf))
                    buf = []
            if buf:
                raw.append("\n".join(buf))
    blocks: list[str] = []
    glue = False
    for p in raw:
        # 标题单独成段时，只粘后面那一段，不要一路粘到底
        if glue and blocks:
            blocks[-1] = blocks[-1] + "\n" + p
            glue = False
            continue
        blocks.append(p)
        glue = bool(re.match(r"^#{1,6}\s", p)) and len(p.splitlines()) == 1
    return blocks


def chinese_len(t: str) -> int:
    return len(re.sub(r"\s", "", t))


def make_chunks(blocks: list[str], size: int, max_paras: int) -> list[list[int]]:
    """返回块的段落下标区间列表。"""
    chunks: list[list[int]] = []
    cur: list[int] = []
    cur_len = 0
    for i, b in enumerate(blocks):
        if cur and (cur_len + chinese_len(b) > size or len(cur) >= max_paras):
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(i)
        cur_len += chinese_len(b)
    if cur:
        chunks.append(cur)
    return chunks


def tail(text: str, n: int = 1) -> str:
    sents = [s for s in re.split(r"(?<=[。！？；])", text) if s.strip()]
    return "".join(sents[-n:]).strip()


def head(text: str, n: int = 1) -> str:
    sents = [s for s in re.split(r"(?<=[。！？；])", text) if s.strip()]
    return "".join(sents[:n]).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("-o", "--out", default="work/review")
    ap.add_argument("--size", type=int, default=500, help="每块目标字数")
    ap.add_argument("--max-paras", type=int, default=5)
    ap.add_argument("--title", default="", help="全文标题/主旨，写进 _index")
    args = ap.parse_args()

    src = Path(args.path)
    text = src.read_text(encoding="utf-8")
    blocks = split_blocks(text)
    chunks = make_chunks(blocks, args.size, args.max_paras)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    index = [
        f"# 审核分块：{src.name}",
        "",
        f"- 总字数：{chinese_len(text)}",
        f"- 段落数：{len(blocks)}",
        f"- 分块数：{len(chunks)}",
        f"- 主旨（人工补）：{args.title or '（待填）'}",
        "",
        "## 块清单",
        "",
        "| 块 | 段 | 字数 | 首句 |",
        "|---|---|---|---|",
    ]

    for n, idx in enumerate(chunks, 1):
        body = "\n\n".join(blocks[i] for i in idx)
        prev_tail = tail(blocks[idx[0] - 1]) if idx[0] > 0 else ""
        next_head = head(blocks[idx[-1] + 1]) if idx[-1] + 1 < len(blocks) else ""
        paras = f"{idx[0] + 1}-{idx[-1] + 1}" if len(idx) > 1 else f"{idx[0] + 1}"
        index.append(f"| c{n:02d} | {paras} | {chinese_len(body)} | {head(body)[:24]} |")

        parts = [
            f"# c{n:02d}（段 {paras}，{chinese_len(body)} 字）",
            "",
            "## 上文接缝（不要审，只用来判断承接）",
            "",
            prev_tail or "（无，这是开头）",
            "",
            "## 本块正文",
            "",
            body,
            "",
            "## 下文接缝（不要审，只用来判断指代）",
            "",
            next_head or "（无，这是结尾）",
            "",
            "## 本块记录",
            "",
            "（按下面格式写，一条一块。写进 reviews/cNN.md）",
            "",
            "### [必改] 段X句Y",
            "原句：",
            "类型：",
            "成本：",
            "依据：",
            "方向：",
            "",
            "### [不要动] 段X句Y",
            "原句：",
            "理由：",
        ]
        (out / f"c{n:02d}.md").write_text("\n".join(parts), encoding="utf-8")

    (out / "_index.md").write_text("\n".join(index) + "\n", encoding="utf-8")

    print(f"切了 {len(chunks)} 块，写到 {out}/")
    print("下一步：先读 _index.md，再逐块审，结论写进 reviews/cNN.md")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
