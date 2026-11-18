#!/usr/bin/env python
"""收尾包闸门：把「这一轮说完了」变成一处能退非零的判据。

为什么要有这个脚本：以前一轮结束了，没有任何一处写着它结束了——`tasks/round.md`
永远停在上一轮的目标上，于是「做完了没有」只能靠人记。入口有六节交接包与
`tools/intake_gate.py`，出口这两样都没有。这个脚本补出口：七节收尾包缺一节、
第 3 节写一个裸「无」、结论写成自由文本、状态位仍是「进行中」、工作区不干净、
库里还有在途工单——任意一条不成立都当场非零并点名缺什么。

用法（在仓库根 {{WORKSPACE}}）：

    python tools/close_gate.py                    # 结构：按 tasks/round.md 的轮次找 tasks/closing-<轮次>.md
    python tools/close_gate.py tasks/closing-R-003.md
    python tools/close_gate.py --check            # 再加三条现场事实
    python tools/close_gate.py --root <仓库> --db <中心库> --project <项目> [--check]

判据：

1. `tasks/round.md` 有 `轮次：R-00N` 与四档之一的状态位；状态是「进行中」时说清
   还没有收尾包。
2. 收尾包存在、UTF-8 可解码、七节齐全且顺序正确。
3. 第 3 节非空，且不是裸「无」「待补充」这类占位（真为零要写「无，因为…」）。
4. 第 7 节第一个非空行是 `已收尾` / `等一句答复` / `需重开一轮` 之一。
5. `--check` 三条现场事实：`git status --porcelain` 为空；中心库里本项目没有在途
   工单（只读打开，照 `tools/session_roster.py` 的写法）；状态位与第 7 节结论一致。

只查形状与三条事实，不查真假：证据是不是真跑过、第 4 节的取舍合不合理，脚本判
不了，那一步由独立验收与人看。**也不执行** `round.md` 里的任意命令——那是代码
执行面，v1 不做。

退出码：通过 0，有问题 1，参数或库有问题 2。
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / ".data" / "task-center.sqlite3"
DEFAULT_PROJECT = "ai-workspace"

# 标题文字与顺序都不许改：闸门按这七行原样匹配，改一个字就是缺节。
SECTIONS = (
    "## 1. 本轮原话目标",
    "## 2. 已完成及证据",
    "## 3. 未做到或偏离",
    "## 4. 决定不做及原因",
    "## 5. 欠 AI 的下一步",
    "## 6. 欠人的一条可执行动作",
    "## 7. 结论",
)
VERDICTS = ("已收尾", "等一句答复", "需重开一轮")
# 状态位四档与三档结论的对应：需重开一轮在 round.md 里写作「需重开」。
STATUSES = ("进行中", "等一句答复", "已收尾", "需重开")
VERDICT_STATUS = {"已收尾": "已收尾", "等一句答复": "等一句答复", "需重开一轮": "需重开"}
# 在途 = 这张单还在跑，与 session_roster.py 用的是同一组值。
IN_FLIGHT = ("dispatching", "running", "awaiting_review", "blocked")

PLACEHOLDER = re.compile(r"^(?:无|暂无|没有|待补充|待定|补充|todo|tbd)[\s。.；;!！?？]*$", re.I)
ROUND_NO = re.compile(r"^轮次[：:]\s*(R-\d{3})\s*$", re.M)
STATUS_LINE = re.compile(r"^状态[：:]\s*(\S+)\s*$", re.M)
HEADING = re.compile(r"^## ")


class GateError(RuntimeError):
    """查不出结论时的唯一异常：带一句人能读的原因，由 main 打到 stderr。"""


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
    """文件里出现的七节标题，按出现顺序返回（其余二级标题不算）。"""
    return [line.strip() for line in text.splitlines() if line.strip() in SECTIONS]


def round_status(root):
    """读 `tasks/round.md`，返回 (轮次, 状态, 问题列表)；读不出来时前两项为 None。"""
    path = Path(root) / "tasks" / "round.md"
    if not path.is_file():
        return None, None, [
            f"{path}: 没有本轮目标文件；收尾要先有 tasks/round.md（轮次 / 状态 / 本轮目标 / 结束条件 / 不做什么）"
        ]
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return None, None, [f"{path}: 不是 UTF-8 文件（第 {exc.start} 字节无法解码）"]

    problems = []
    match = ROUND_NO.search(text)
    round_no = match.group(1) if match else None
    if round_no is None:
        problems.append(
            f"{path}: 顶部没有 `轮次：R-00N` 这一行；没有编号的一轮在 tasks/rounds.md 里对不上账"
        )

    status_match = STATUS_LINE.search(text)
    status = status_match.group(1) if status_match else None
    if status is None:
        problems.append(f"{path}: 顶部没有 `状态：…` 这一行；只收四档之一：{'、'.join(STATUSES)}")
    elif status not in STATUSES:
        problems.append(
            f"{path}: 状态写成 `{status}`，只收四档之一：{'、'.join(STATUSES)}"
        )
        status = None
    return round_no, status, problems


def inspect_package(path):
    """查收尾包的结构，返回 (问题列表, 结论或 None)。结论只认三档之一。"""
    path = Path(path)
    if not path.is_file():
        return [
            f"{path}: 收尾包不存在；七节结构与落点见 docs/closing.md（写 tasks/closing-<轮次>.md）"
        ], None
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return [f"{path}: 不是 UTF-8 文件（第 {exc.start} 字节无法解码）；收尾包存成 UTF-8 再收"], None
    if not text.strip():
        return [f"{path}: 文件为空"], None

    problems = []
    found = sections(text)

    for heading in SECTIONS:
        if heading not in found:
            number = heading.split(".")[0].lstrip("# ")
            problems.append(f"{path}: 缺少第 {number} 节 `{heading}`；七节结构与判据见 docs/closing.md")

    order = heading_order(text)
    for previous, current in zip(order, order[1:]):
        if SECTIONS.index(current) <= SECTIONS.index(previous):
            problems.append(f"{path}: 节的顺序不对：`{current}` 出现在 `{previous}` 之后；七节顺序固定")

    third = found.get(SECTIONS[2], "").strip()
    if SECTIONS[2] in found:
        if not third:
            problems.append(
                f"{path}: 第 3 节是空的；没做到、做到一半、与原话目标不一致的都要写，"
                "真为零就写「无，因为每条目标都有对应证据，见第 2 节」"
            )
        elif PLACEHOLDER.match(third):
            problems.append(
                f"{path}: 第 3 节只写了「{third}」；一个裸「无」看不出你确认过，"
                "真为零要写「无，因为每条目标都有对应证据，见第 2 节」"
            )

    verdict = None
    seventh = found.get(SECTIONS[6])
    if seventh is not None:
        head = re.split(r"[。.；;\n]", seventh.strip(), maxsplit=1)[0].strip()
        if head in VERDICTS:
            verdict = head
        else:
            problems.append(
                f"{path}: 第 7 节的结论写成 `{head or '（空）'}`，只收三档之一：{'、'.join(VERDICTS)}；"
                "写成自由文本就判不出下一步该干什么"
            )

    return problems, verdict


def check(root, package=None):
    """结构检查（不需要库）。`package` 省略时按 round.md 的轮次定位收尾包。"""
    root = Path(root)
    round_no, status, problems = round_status(root)

    if package is not None:
        path = Path(package)
    elif round_no is not None:
        path = root / "tasks" / f"closing-{round_no}.md"
    else:
        path = None

    if status == "进行中":
        target = path if path is not None else root / "tasks" / "closing-<轮次>.md"
        problems.append(
            f"{target}: 本轮状态是 进行中，还没有收尾包；先按 docs/closing.md 写七节包，"
            f"再把 {root / 'tasks' / 'round.md'} 的状态位改成 已收尾 / 等一句答复 / 需重开"
        )

    if path is None:
        return problems

    package_problems, _verdict = inspect_package(path)
    return problems + package_problems


def main_checkout(root):
    """工作树里跑时，找出它对应的主检出根（中心库在主检出那边）。

    `git rev-parse --git-common-dir` 在主检出里回答 `<根>/.git`，在工作树里也回答
    主检出那个 `.git`——工作树的 `.git` 只是一个文本指针。所以「它的父目录」就是
    主检出，中心库在主检出的 `.data/` 下。找不到就返回 None，不猜。
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    common = Path(result.stdout.strip())
    return common.parent if common.name == ".git" else common


def default_db(root):
    """中心库的默认落点：先看这个检出，再看主检出；都没有就报第一个候选。"""
    candidates = [Path(root) / ".data" / "task-center.sqlite3"]
    main = main_checkout(root)
    if main is not None:
        candidates.append(main / ".data" / "task-center.sqlite3")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def read_only(path):
    path = Path(path)
    if not path.is_file():
        raise GateError(f"中心库不存在：{path}")
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.execute("PRAGMA query_only=ON")
        return conn
    except sqlite3.Error as exc:
        raise GateError(f"打不开中心库 {path}：{exc}") from exc


def in_flight_tickets(db, project):
    conn = read_only(db)
    try:
        return conn.execute(
            "SELECT id, state FROM tickets WHERE project_id=? AND state IN (?,?,?,?) ORDER BY id",
            (project, *IN_FLIGHT),
        ).fetchall()
    except sqlite3.Error as exc:
        raise GateError(f"查不了 {db} 的工单（目标不是本工具的库？）：{exc}") from exc
    finally:
        conn.close()


def check_facts(root, db, project=DEFAULT_PROJECT):
    """`--check` 的三条现场事实，各自独立成句。"""
    root = Path(root)
    problems = []

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        problems.append(f"{root}: 跑不了 `git status`（{exc}）；三条现场事实要在一个 git 检出上判")
    else:
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()[:1]
            problems.append(
                f"{root}: 不是 git 检出或 git 报错（{detail[0] if detail else result.returncode}）；"
                "三条现场事实要在一个 git 检出上判"
            )
        else:
            dirty = [line for line in result.stdout.splitlines() if line.strip()]
            if dirty:
                sample = "、".join(line[3:].strip() or line.strip() for line in dirty[:5])
                more = f" 等 {len(dirty)} 处" if len(dirty) > 5 else ""
                problems.append(
                    f"{root}: 工作区不干净（{sample}{more}）；成果在磁盘上、不在版本库里，先说清它归哪一次提交"
                )

    rows = in_flight_tickets(db, project)
    if rows:
        listed = "、".join(f"{ticket_id}（{state}）" for ticket_id, state in rows)
        problems.append(
            f"{db}: 项目 {project} 还有 {len(rows)} 张在途工单：{listed}；"
            "本项目不处于可收尾状态，先等它验收或走 recover_ticket"
        )

    round_no, status, _round_problems = round_status(root)
    if round_no is not None and status is not None:
        package = root / "tasks" / f"closing-{round_no}.md"
        _package_problems, verdict = inspect_package(package)
        if verdict is not None and VERDICT_STATUS.get(verdict) != status:
            expected = VERDICT_STATUS[verdict]
            problems.append(
                f"{package}: 状态位写的是 `状态：{status}`，收尾包第 7 节的结论是 `{verdict}`"
                f"（应写 `状态：{expected}`）；两处不一致，这一轮算不算完就没人说了算"
            )

    return problems


def main(argv=None):
    try:  # Windows 控制台默认 GBK，中文输出重定向就变乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(
        description="收尾包闸门：七节结构（默认）加三条现场事实（--check）。"
    )
    parser.add_argument(
        "package",
        nargs="?",
        help="收尾包路径；省略时按 tasks/round.md 的轮次取 tasks/closing-<轮次>.md",
    )
    parser.add_argument("--root", default=None, help=f"仓库根（默认 {ROOT}）")
    parser.add_argument(
        "--db",
        default=None,
        help=f"中心库（默认本检出的 {DEFAULT_DB}，工作树里退回主检出的 .data/）",
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT, help=f"项目标识（默认 {DEFAULT_PROJECT}）")
    parser.add_argument("--check", action="store_true", help="再加三条现场事实：工作区、在途工单、状态与结论")
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else ROOT
    db = Path(args.db) if args.db else default_db(root)

    try:
        problems = check(root, args.package)
        if args.check:
            problems.extend(check_facts(root, db, args.project))
    except GateError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if problems:
        print(f"收尾包闸门未通过，{len(problems)} 个问题：", file=sys.stderr)
        for problem in problems:
            print(" - " + problem, file=sys.stderr)
        print("非零就是不许说「这一轮结束」；判据与落点见 docs/closing.md。", file=sys.stderr)
        return 1

    print("闸门通过：七节齐全，可以做收尾宣告（--check 另跑三条现场事实）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
