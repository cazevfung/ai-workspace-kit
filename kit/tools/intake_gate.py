"""交接包闸门：把「交接时该写下来的判断」变成可失败的检查。

为什么要有这个脚本：交接协议以前只要求一条三行消息，背景整体托付给收件方
去读原对话。历史里最容易丢的恰恰不是功能，是判断——用户说过「上一次哪里
不行」「这次什么算好」「什么绝不能被改掉」。这些话在转抄时不像功能名那样
有位置可放，于是最先被丢；丢完之后产物仍然写得完备，验收也仍然能全绿。
2026-11-18 的 1205 配乐交接就是这条路（`tests/fixtures/intake/` 里那两份
对照样本）。六节结构给判断留了非填不可的位置，这个脚本负责让它当场失败。

只查形状，不查真假：原话有没有被删减、写的是不是真事，脚本判不了。那一步
由协调会话用 `get_chat_history` 读原对话抽查。两件事不能互相替代。

用法：

    python tools/intake_gate.py 交接包.md
    python tools/intake_gate.py --extract 交接包.md   # 只打印第 1 节，供 PRD 原样搬运

判据五条（口径与 `docs/intake.md` 一致，不在别处另写一份）：文件存在且能按 UTF-8 解码；六个固定标题齐全且
顺序正确；第 1 节含至少一段以 `>` 开头的引用；第 3 节非空且不是「待补充」这
类占位；第 5 节至少有一个绝对路径。

退出码：通过 0，有问题 1，用法错误 2。
"""
import re
import sys
from pathlib import Path

# 标题文字与顺序都不许改：闸门按这六行原样匹配，改一个字就是缺节。
SECTIONS = (
    "## 1. 用户原话",
    "## 2. 要交付什么",
    "## 3. 做过什么、为什么不行",
    "## 4. 硬约束",
    "## 5. 已确认的事实",
    "## 6. 未决问题",
)
QUOTE = re.compile(r"^\s*>", re.M)
PLACEHOLDER = re.compile(r"^(?:待补充|待定|补充|todo|tbd)[\s。.；;!！?？]*$", re.I)
ABS_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)")
HEADING = re.compile(r"^## ")


def sections(text):
    """把 `## ` 二级标题切成 {标题: 正文}，保留首次出现的那一段。"""
    found = {}
    current = None
    buffer = []
    for line in text.splitlines():
        if HEADING.match(line):
            if current is not None and current not in found:
                found[current] = "\n".join(buffer)
            current = line.strip()
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None and current not in found:
        found[current] = "\n".join(buffer)
    return found


def heading_order(text):
    """文件里出现的六节标题，按出现顺序返回（其余二级标题不算）。"""
    return [line.strip() for line in text.splitlines() if line.strip() in SECTIONS]


def check(path):
    """返回问题列表；空列表表示通过。不做语义判定，只判形状。"""
    path = Path(path)
    if not path.is_file():
        return [
            f"{path}: 文件不存在；交接包要真的落盘再发消息——有项目时写 `交接/<日期>-<主题>.md`，"
            "没有绑定项目时写 `{{WORKSPACE}}/.data/intake/<会话标识>-<日期>.md`"
        ]
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return [f"{path}: 不是 UTF-8 文件（第 {exc.start} 字节无法解码）；交接包存成 UTF-8 再发"]
    if not text.strip():
        return [f"{path}: 文件为空"]

    problems = []
    found = sections(text)

    for heading in SECTIONS:
        if heading not in found:
            number = heading.split(".")[0].lstrip("# ")
            problems.append(f"{path}: 缺少第 {number} 节 `{heading}`；六节结构与判据见 docs/intake.md §2")

    order = heading_order(text)
    for previous, current in zip(order, order[1:]):
        if SECTIONS.index(current) <= SECTIONS.index(previous):
            problems.append(f"{path}: 节的顺序不对：`{current}` 出现在 `{previous}` 之后；六节顺序固定")

    first = found.get(SECTIONS[0], "")
    if SECTIONS[0] in found and not QUOTE.search(first):
        problems.append(
            f"{path}: 第 1 节没有引用块；用户原话要逐字写成 `> …`，不改写、不摘要——"
            "这一节缺了，「上次哪里不行」就没有落点"
        )

    third = found.get(SECTIONS[2], "").strip()
    if SECTIONS[2] in found:
        if not third:
            problems.append(
                f"{path}: 第 3 节是空的；上一版做了什么、用户怎么评价、这次要改哪一层都要写，"
                "没有前史就写「无前史」并说明依据"
            )
        elif PLACEHOLDER.match(third):
            problems.append(
                f"{path}: 第 3 节只写了占位「{third}」；这一节是这次事故的病根，不许留空话"
            )

    fifth = found.get(SECTIONS[4], "")
    if SECTIONS[4] in found and not ABS_PATH.search(fifth):
        problems.append(
            f"{path}: 第 5 节没有绝对路径；已确认的事实要给到 `X:/…`、`X:\\…` 或 `\\\\…` 这种可核对的落点"
        )

    return problems


def extract(path):
    """返回第 1 节正文；文件不合规时返回 None（不以半成品供 PRD 搬运）。"""
    if check(path):
        return None
    return sections(Path(path).read_bytes().decode("utf-8-sig"))[SECTIONS[0]].strip()


def usage():
    return "用法：python tools/intake_gate.py [--extract] <交接包.md>"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    want_extract = "--extract" in argv
    if want_extract:
        argv.remove("--extract")
    if len(argv) != 1 or argv[0].startswith("-"):
        print(f"{usage()}\n", file=sys.stderr)
        print(__doc__.strip(), file=sys.stderr)
        return 2

    path = Path(argv[0])
    problems = check(path)
    if problems:
        print(f"交接包闸门未通过，{len(problems)} 个问题：", file=sys.stderr)
        for problem in problems:
            print(" - " + problem, file=sys.stderr)
        return 1

    if want_extract:
        print(extract(path))
    else:
        print(f"闸门通过：{path} 六节齐全")
    return 0


if __name__ == "__main__":
    sys.exit(main())
