#!/usr/bin/env python
"""断言核对器：把「文档里写的数字」变成一次能失败的复算。

为什么要有这个脚本：结案材料里的数字（计数、合计、比例、摘要、体积、锚点、
清单条数）以前只能靠人对眼。对不上时没有任何一处会非零退出，所以「数字错了」
从来不是一次失败，而是一次疏忽。这个脚本把这一类检查变成退出码。

四类断言，七种 `kind`：

    数量类    count   文档里命中某个模式的行数
              sum     文档里若干行抓出的数字之总和
              ratio   两个模式的行数之比，按 digits 位小数比
    摘要类    sha256  文件实际摘要与登记值比
              size    文件实际字节数与登记值比
    位置类    anchor  文档第 N 行是否存在、是否包含指定文本
    清单类    list    某个二级标题节下的清单条目数

只核数量、哈希、体积、锚点与清单条数：口径合理不合理、证据有没有可能失败、
某个数该不该出现在文档里，一件都不判。判定类的判断在这里永远是缺席的，
它判的是「写下来的数和源数据对不对得上」。

期望值从哪儿来：`claims.json` 里的 `expect` 必须带一条 `source`，写明这个值是
从句命令算出来的、还是从哪一行的源数据抄来的。缺 `source` 按格式错误处理。
这条不是礼貌要求——手填的期望值等于把错数从文档搬到 claims.json，
搬过去之后闸门就成了唯一一处「知道正确答案」的地方，而它并不知道。

用法：

    python tools/claims_gate.py --claims <claims.json>
    python tools/claims_gate.py --claims <claims.json> --emit   # 只打印实测值，供回填
    python tools/claims_gate.py --coverage --claims <claims.json> --paths <目录或文件>...
    python tools/claims_gate.py --help

`--coverage` 是清单，不是判据：它扫 `--paths` 给的文档，把里面出现、但没有任何
claim 覆盖的活数位置一行一条列出来（`路径:行号` + 原样那一行），**退出码恒为 0**。
它只指出「没有人登记过核对」的位置，不判这个数该不该在、也不判口径对不对。
三种位置按下面的形状标出来（同一行可能同时是几种）：

    裸数      没有量词、也不在 `文件:行号` 引用里的数字
    数+量词   同一行里成对出现的数与量词（「29 处」「7 条」「3 个」）
    文件:行号 文档里写出的 `xxx.py:131` 这类引用

覆盖怎么算（机械规则，不判语义）：一条 claim 覆盖它点名的 `file` + `line`；`sha256` /
`size` 这类整文件断言覆盖该文件全部行；`count` / `sum` / `ratio` 的 pattern 与 `list` 的
节内条目覆盖它们命中的行。代码围栏里的示例数字不算正文，不列入。

`file` 相对 `claims.json` 所在目录解析，所以一份 claims.json 可以放在项目根，
也可以与它核的文档放在一起；`--paths` 相对当前工作目录解析。

退出码：全部对上 0；有对不上的 1；用法错误或 claims 文件本身的形状有问题 2。
`--coverage` 只在 claims 文件本身有问题时返回 2，清单有多少条都返回 0。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

# 七种 kind 归到四类；顺序固定，`--help` 与本文件顶部说明用同一份。
KINDS = {
    "count": "数量类",
    "sum": "数量类",
    "ratio": "数量类",
    "sha256": "摘要类",
    "size": "摘要类",
    "anchor": "位置类",
    "list": "清单类",
}

# 每种 kind 必须给的字段（`id` / `kind` / `file` / `source` 四件每种都要，另算）。
REQUIRED = {
    "count": ("pattern", "expect"),
    "sum": ("pattern", "expect"),
    "ratio": ("numerator", "denominator", "expect"),
    "sha256": ("expect",),
    "size": ("expect",),
    "anchor": ("line", "contains"),
    "list": ("section", "expect"),
}

# 摘要是定长大写十六进制以外的写法一律按格式错误：真比对前先让格式对得上。
HEX64 = re.compile(r"^[0-9a-f]{64}$")
# 清单条目默认按行首记号认（`- ` / `* `），见 docs/claims-format.md 的口径说明。
DEFAULT_ITEM_MARKER = r"^[-*] "


class ClaimsError(Exception):
    """claims 文件本身有问题（形状、字段、类型），不是某条断言对不上。"""


def lines_of(path):
    """按行读文本；`read_text` 会做通用换行转换，CRLF 与 LF 都按同一份行数算。"""
    return path.read_text(encoding="utf-8").splitlines()


def hits(lines, pattern, regex=True):
    """命中的行号列表（1 起）。`regex=False` 时按子串找，不编译正则。"""
    if not regex:
        return [no for no, line in enumerate(lines, 1) if pattern in line]
    compiled = re.compile(pattern)
    return [no for no, line in enumerate(lines, 1) if compiled.search(line)]


def sum_of(lines, pattern):
    """把每行第一个捕获组当整数加起来。组数不是 1 就无法确定加什么。"""
    compiled = re.compile(pattern)
    if compiled.groups != 1:
        raise ClaimsError(f"sum 的 pattern 必须正好带一个捕获组，现在是 {compiled.groups} 个：{pattern}")
    total = 0
    for line in lines:
        match = compiled.search(line)
        if match:
            total += int(match.group(1))
    return total


def section_item_lines(lines, section, marker):
    """某个二级（及以下）标题节下命中 `marker` 的行号（1 起）；找不到这一节返回 None。"""
    compiled = re.compile(marker)
    start = None
    for index, line in enumerate(lines):
        if line.startswith("#") and line.lstrip("#").strip() == section:
            start = index
            break
    if start is None:
        return None
    found = []
    for offset, line in enumerate(lines[start + 1 :], start + 2):
        if line.startswith("#"):
            break
        if compiled.search(line):
            found.append(offset)
    return found


def section_items(lines, section, marker):
    """某个二级（及以下）标题节下的清单条目数；找不到这一节返回 None。"""
    found = section_item_lines(lines, section, marker)
    return None if found is None else len(found)


def where(claim):
    """报错定位：`文件:行号`。

    整份文件的断言（sha256 / size）没有具体行号，取第 1 行；这样每条断言的报错
    都能按同一个形状点到文档里的一处，不用分两种输出格式。
    """
    name = claim.get("file") or "<未指定文件>"
    line = claim.get("line")
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        line = 1
    return f"{name}:{line}"


def validate(claim, index, need_expect=True):
    """把一条断言检查到可以开跑为止；不合法就抛 ClaimsError。"""
    if not isinstance(claim, dict):
        raise ClaimsError(f"第 {index} 条断言不是对象")
    claim_id = claim.get("id")
    if not isinstance(claim_id, str) or not claim_id.strip():
        raise ClaimsError(f"第 {index} 条断言缺 `id`；报错要点名是哪一条")

    kind = claim.get("kind")
    if kind not in KINDS:
        raise ClaimsError(
            f"{claim_id}: 不认识的 kind `{kind}`；只收 {' / '.join(KINDS)}"
        )

    for key in ("kind", "file", "source") + REQUIRED[kind]:
        if key in ("expect",) and not need_expect:
            continue
        if key not in claim or claim[key] in (None, ""):
            raise ClaimsError(f"{claim_id}: kind={kind} 缺字段 `{key}`")

    if not isinstance(claim["file"], str) or not claim["file"].strip():
        raise ClaimsError(f"{claim_id}: `file` 必须是非空字符串")

    if "line" in claim:
        line = claim["line"]
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            raise ClaimsError(f"{claim_id}: `line` 必须是 ≥1 的整数，现在是 {line!r}")

    # anchor 不登记期望值：它本身就是「这一行是不是这样」的核对，实测即结论。
    if not need_expect or "expect" not in claim:
        return
    expect = claim["expect"]
    if kind == "sha256":
        if not isinstance(expect, str) or not HEX64.match(expect):
            raise ClaimsError(
                f"{claim_id}: sha256 的期望值必须是 64 位小写十六进制（现在 {expect!r}）；"
                "大小写不一致会让同一个摘要在两份文件里看起来不同"
            )
    elif isinstance(expect, bool) or not isinstance(expect, (int, float)):
        raise ClaimsError(f"{claim_id}: {kind} 的期望值必须是数字，现在是 {expect!r}")


def evaluate(claim, base):
    """算一条断言的实测值，返回 (实测值, 问题列表)。问题非空就是这条不成立。"""
    location = where(claim)
    target = base / claim["file"]
    if not target.is_file():
        return None, [f"{location}: 目标文件不存在：{claim['file']}"]
    kind = claim["kind"]

    if kind in ("count", "sum", "ratio", "list"):
        lines = lines_of(target)
    elif kind == "anchor":
        lines = lines_of(target)

    if kind == "count":
        return len(hits(lines, claim["pattern"], bool(claim.get("regex", True)))), []

    if kind == "sum":
        return sum_of(lines, claim["pattern"]), []

    if kind == "ratio":
        numerator = len(hits(lines, claim["numerator"]))
        denominator = len(hits(lines, claim["denominator"]))
        if denominator == 0:
            return None, [
                f"{location}: 分母为 0——`{claim['denominator']}` 在文档里一次也没命中，比例无从算起"
            ]
        digits = claim.get("digits", 2)
        if isinstance(digits, bool) or not isinstance(digits, int) or digits < 0:
            raise ClaimsError(f"{claim['id']}: `digits` 必须是 ≥0 的整数")
        return round(numerator / denominator, digits), []

    if kind == "sha256":
        return hashlib.sha256(target.read_bytes()).hexdigest(), []

    if kind == "size":
        return target.stat().st_size, []

    if kind == "anchor":
        line = claim["line"]
        if line > len(lines):
            return None, [
                f"{location}: `{claim['file']}` 只有 {len(lines)} 行，锚点指向第 {line} 行"
            ]
        text = lines[line - 1]
        if claim["contains"] not in text:
            return None, [
                f"{location}: 第 {line} 行不含 `{claim['contains']}`；这一行实际是 `{text}`"
            ]
        return text.strip(), []

    if kind == "list":
        count = section_items(lines, claim["section"], claim.get("marker", DEFAULT_ITEM_MARKER))
        if count is None:
            return None, [
                f"{location}: 没有找到标题为 `{claim['section']}` 的节，清单条目数无从数起"
            ]
        return count, []

    raise ClaimsError(f"{claim['id']}: 不认识的 kind `{kind}`")


def mismatch(claim, observed):
    """实测与登记的差在哪儿，一句话说清（数字与摘要的措辞不同）。"""
    expect = claim["expect"]
    if claim["kind"] == "sha256":
        return f"{where(claim)}: sha256 不一致：登记 {expect}，实测 {observed}"
    return f"{where(claim)}: {claim['kind']} 不一致：登记 {expect}，实测 {observed}"


def load(path):
    """读 claims.json，返回 (断言列表, 它所在目录)。"""
    path = Path(path)
    if not path.is_file():
        raise ClaimsError(f"{path}: claims 文件不存在")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ClaimsError(f"{path}: 不是合法 JSON（第 {exc.lineno} 行：{exc.msg}）") from exc
    if not isinstance(payload, dict):
        raise ClaimsError(f"{path}: 顶层必须是对象，形如 {{\"version\": 1, \"claims\": [...]}}")
    version = payload.get("version", 1)
    if version != 1:
        raise ClaimsError(f"{path}: 只认 version 1，现在是 {version!r}")
    claims = payload.get("claims")
    if not isinstance(claims, list) or not claims:
        raise ClaimsError(f"{path}: `claims` 必须是非空数组")
    return claims, path.resolve().parent


def show_path(path):
    """清单里显示的路径：在 cwd 之下就用相对路径，否则用绝对路径；一律正斜杠。"""
    path = Path(path).resolve()
    try:
        return path.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def body_lines(path):
    """正文行 `(行号, 原样内容)`：代码围栏的标记行与内部行都不算正文。

    围栏里的数字是示例（`value = 37`、一段 JSON），不是文档里写给人看的活数；
    把它们列进来只会把真位置淡在噪声里。行号仍按原文件算，删行后仍能对上。
    """
    kept = []
    inside = False
    for number, text in enumerate(lines_of(path), 1):
        stripped = text.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            inside = not inside
            continue
        if not inside:
            kept.append((number, text))
    return kept


# 三种位置。顺序就是清单里标签的顺序；同一个位置命中几种就标几种。
KIND_BARE = "裸数"
KIND_UNIT = "数+量词"
KIND_REF = "文件:行号"

REFERENCE = re.compile(r"[A-Za-z0-9_./\\-]*\.(?:md|py|json|txt|sqlite3|yml|yaml):\d+(?:-\d+)?")
NUMBER_WITH_UNIT = re.compile(
    r"\d+(?:\.\d+)?\s*(?:处|条|项|个|份|次|行|家|种|类|张|页|人|天|周|位|节|层|列|步|版|字符|字|秒|分钟|小时|倍|%|％)"
)
# 前后贴字母/数字/下划线/点/斜杠/连字符的不算裸数：`PRD-001`、`v1.2`、`2026-11-17`
# 这类是标识与日期，把它们列进清单会淹掉真的活数。
BARE_NUMBER = re.compile(r"(?<![A-Za-z0-9_./\\-])\d+(?:\.\d+)?(?![A-Za-z0-9_-])")


# 行首序号（`1. `、`2、`、`### 8.1 `）不是活数：它数的是列表第几项，不是文档里的量。
# 只在后面真的跟了分隔符时才算序号，否则「37 处」开头的行会被当成序号吃掉。
HEADING_ORDINAL = re.compile(r"^#{1,6}\s+\d+(?:\.\d+)*\.?\s+")
ORDINAL = re.compile(r"^\d+(?:\.\d+)*[.、)]\s+")


def position_kinds(text):
    """这一行里有哪几种活数位置；一个都没有就返回空列表。"""
    kinds = []
    rest = HEADING_ORDINAL.sub("", text, count=1)
    if rest is text:
        rest = ORDINAL.sub("", text, count=1)
    if REFERENCE.search(rest):
        kinds.append(KIND_REF)
        rest = REFERENCE.sub(" ", rest)
    if NUMBER_WITH_UNIT.search(rest):
        kinds.append(KIND_UNIT)
        rest = NUMBER_WITH_UNIT.sub(" ", rest)
    if BARE_NUMBER.search(rest):
        kinds.append(KIND_BARE)
    return kinds


def claim_lines(claim):
    """一条 claim 说它管了哪些行号（不含它点名的 `line`，那是另一条规则）。"""
    kind = claim["kind"]
    if kind in ("sha256", "size", "anchor"):
        return []
    if kind == "list":
        found = section_item_lines(
            lines_of(Path(claim["_target"])),
            claim["section"],
            claim.get("marker", DEFAULT_ITEM_MARKER),
        )
        return found or []
    patterns = (
        [claim["numerator"], claim["denominator"]] if kind == "ratio" else [claim["pattern"]]
    )
    lines = lines_of(Path(claim["_target"]))
    matched = []
    for pattern in patterns:
        try:
            compiled = re.compile(pattern)
        except re.error:
            continue
        if kind == "count" and claim.get("regex", True) is False:
            matched.extend(no for no, text in enumerate(lines, 1) if pattern in text)
        else:
            matched.extend(no for no, text in enumerate(lines, 1) if compiled.search(text))
    return matched


def covered_positions(claims, base):
    """返回 (被点名的位置集合, 被整文件断言盖住的文件集合)。"""
    positions = set()
    whole_files = set()
    for claim in claims:
        target = (base / claim["file"]).resolve()
        if claim["kind"] in ("sha256", "size"):
            whole_files.add(target)
            continue
        line = claim.get("line")
        if isinstance(line, int) and not isinstance(line, bool) and line >= 1:
            positions.add((target, line))
        if not target.is_file():
            continue
        claim["_target"] = target
        for number in claim_lines(claim):
            positions.add((target, number))
    return positions, whole_files


def collect_files(paths):
    """`--paths` 展开成文件列表：目录扫它下面的 `*.md`（递归），文件则原样收。"""
    found = []
    problems = []
    for raw in paths:
        target = Path(raw)
        if target.is_dir():
            found.extend(sorted(p for p in target.rglob("*.md") if p.is_file()))
        elif target.is_file():
            found.append(target)
        else:
            problems.append(f"--paths 里的路径不存在：{raw}")
    return found, problems


def coverage(claims, base, paths, claims_label):
    """打印覆盖率清单。永不因清单内容非零退出——它是给人看的，不是闸门。"""
    files, problems = collect_files(paths)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 2

    positions, whole_files = covered_positions(claims, base)
    entries = []
    line_total = 0
    for path in files:
        target = path.resolve()
        body = body_lines(path)
        line_total += len(body)
        if target in whole_files:
            continue
        for number, text in body:
            kinds = position_kinds(text)
            if not kinds or (target, number) in positions:
                continue
            entries.append((show_path(path), number, kinds, text.rstrip()))

    print(f"覆盖率清单（只打印，不判定）：{claims_label}")
    print(
        f"扫描 {len(files)} 个文件、{line_total} 行正文（代码围栏不计）；claims {len(claims)} 条"
    )
    for name, number, kinds, text in entries:
        print(f"{name}:{number}\t[{'/'.join(kinds)}] {text}")
    print(f"未被覆盖 {len(entries)} 处")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__.strip())
        return 2
    if argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0

    parser = argparse.ArgumentParser(add_help=False, prog="claims_gate.py")
    parser.add_argument("--claims", required=True)
    parser.add_argument("--emit", action="store_true")
    parser.add_argument("--coverage", action="store_true")
    parser.add_argument("--paths", nargs="+")
    try:
        args, rest = parser.parse_known_args(argv)
    except SystemExit:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    if rest:
        print(f"不认识的参数：{' '.join(rest)}\n", file=sys.stderr)
        print(__doc__.strip(), file=sys.stderr)
        return 2
    if args.coverage and args.emit:
        print("--coverage 与 --emit 不能同时用：前者列位置，后者索实测值\n", file=sys.stderr)
        return 2
    if args.coverage and not args.paths:
        print("--coverage 必须配 --paths：没有要扫的文档，清单列什么\n", file=sys.stderr)
        return 2

    try:
        claims, base = load(args.claims)
        for index, claim in enumerate(claims, 1):
            validate(claim, index, need_expect=not args.emit)
    except ClaimsError as exc:
        print(f"claims 文件有问题：{exc}", file=sys.stderr)
        return 2

    if args.coverage:
        return coverage(claims, base, args.paths, args.claims)

    if args.emit:
        print(f"断言核对（--emit，只打印实测值，不与登记值比）：{args.claims}")
        failed = 0
        for claim in claims:
            try:
                observed, problems = evaluate(claim, base)
            except ClaimsError as exc:
                print(f"claims 文件有问题：{exc}", file=sys.stderr)
                return 2
            if problems:
                failed += 1
                for problem in problems:
                    print(" - " + problem)
                continue
            print(f"{claim['id']}\t{claim['kind']}\t{observed}")
        print(f"实测 {len(claims) - failed} / {len(claims)}")
        return 1 if failed else 0

    print(f"断言核对：{args.claims}")
    problems = []
    for claim in claims:
        try:
            observed, found = evaluate(claim, base)
        except ClaimsError as exc:
            print(f"claims 文件有问题：{exc}", file=sys.stderr)
            return 2
        if found:
            problems.extend(found)
        elif "expect" in claim and observed != claim["expect"]:
            problems.append(mismatch(claim, observed))

    if problems:
        for problem in problems:
            print(" - " + problem)
        print(f"未通过 {len(problems)} / {len(claims)}")
        return 1
    print(f"通过 {len(claims)} / {len(claims)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
