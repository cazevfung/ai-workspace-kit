#!/usr/bin/env python3
"""校验「接入」技能的 description 与正文，并把「改了什么」变成可当场重跑的东西。

这些 `SKILL.md` 在 `~/.agents/skills/` 下，不在任何版本控制里。仓库这边只有三样：
快照、期望词表、这个脚本。脚本回答三件事，答不上来就非零退出：

  1. desc   —— live 的 `description:` 与词表里的 `approved` 一致（上游把它冲掉就报
               `description-drifted`）。description 的权威文本在词表里，不在快照里。
  2. terms  —— `description` 含词表登记的全部中文触发词，长度在上限内
  3. body   —— 除 `description:` 那一行外，live 与快照逐字节一致

快照只负责「正文有没有被改掉」：`index/snapshot/<名>/SKILL.md` 就是 live 的逐字节副本
（连 `description:` 行一起照抄），所以正文一改就报 `body-changed`，不必虚构任何
「改前描述」——description 的判据在 `approved` 上，与快照无关。

名单不手抄：从 `installed-skills.md` 的「本机通用 + 接入」行读，与词表逐项对，
多一个少一个都算 `scope-mismatch`；技能个数从词表读出，不写死。

用法：

    python .agents/skills/skill-routing/scripts/check_index.py
    python .agents/skills/skill-routing/scripts/check_index.py --json
    python .../check_index.py --only body     # 只查正文未变（按快照还原后用这条）
    python .../check_index.py --only desc     # 只查 description 是否偏离 approved
    python .../check_index.py --skills-root <目录> --index-root <目录> --routing-dir <目录>

退出码：0 全部通过；1 发现问题；2 用法错误。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_INDEX_ROOT = Path(__file__).resolve().parent.parent / "index"
DEFAULT_ROUTING_DIR = Path(__file__).resolve().parent.parent
MODES = ("all", "desc", "terms", "body")
SOURCE_CELL = "本机通用"
ATTACH_CELL = "接入"
MAX_DIFF_LINES = 5


def _utf8_streams() -> None:
    """Windows 控制台默认按本地代码页编码，中文输出会变乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


class Problem:
    def __init__(self, kind: str, skill: str, message: str, line: int | None = None) -> None:
        self.kind = kind
        self.skill = skill
        self.message = message
        self.line = line

    def render(self) -> str:
        if self.line is None:
            return "[%s] %s: %s" % (self.kind, self.skill, self.message)
        return "[%s] %s: 第 %d 行 %s" % (self.kind, self.skill, self.line, self.message)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "skill": self.skill, "line": self.line, "message": self.message}


def split_frontmatter(text: str) -> tuple[list[str], int]:
    """返回（按行保留行尾的列表，description 行下标）。找不到就是文件结构坏了。"""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("frontmatter 不以 --- 开头")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise ValueError("frontmatter 没有结束的 ---")
    hits = [i for i in range(1, end) if lines[i].lstrip().startswith("description:")]
    if len(hits) != 1:
        raise ValueError("frontmatter 里有 %d 处 description:" % len(hits))
    return lines, hits[0]


def description_of(lines: list[str], index: int) -> str:
    raw = lines[index].strip()
    return raw[len("description:"):].strip()


def body_of(lines: list[str], index: int) -> list[tuple[int, str]]:
    """正文 = 整个文件去掉 description 那一行，保留行尾；带原始 1-based 行号，便于报差异行。"""
    return [(i + 1, lines[i]) for i in range(len(lines)) if i != index]


def load_expected(index_root: Path) -> dict:
    path = index_root / "expected-terms.json"
    if not path.is_file():
        raise SystemExit("用法错误：找不到 %s" % path)
    return json.loads(path.read_text(encoding="utf-8"))


def attached_skills(routing_dir: Path) -> list[str]:
    """从处置表读「本机通用 + 接入」的 skill 名，这是 PRD 规定的唯一名单来源。"""
    path = routing_dir / "installed-skills.md"
    if not path.is_file():
        raise SystemExit("用法错误：找不到 %s" % path)
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[1] == SOURCE_CELL and cells[2] == ATTACH_CELL:
            names.append(cells[0].strip("`"))
    return names


def check_scope(expected: dict, routing_dir: Path) -> list[Problem]:
    problems = []
    listed = [item["name"] for item in expected.get("skills", [])]
    wanted = attached_skills(routing_dir)
    for name in wanted:
        if name not in listed:
            problems.append(Problem("scope-mismatch", name, "在处置表里是本机通用 / 接入，但词表没登记"))
    for name in listed:
        if name not in wanted:
            problems.append(Problem("scope-mismatch", name, "词表登记了，但处置表里不是本机通用 / 接入"))
    if len(listed) != len(set(listed)):
        problems.append(Problem("scope-mismatch", "(词表)", "同一个 skill 登记了不止一次"))
    return problems


def check_skill(entry: dict, skills_root: Path, index_root: Path, mode: str) -> list[Problem]:
    name = entry["name"]
    problems: list[Problem] = []
    snapshot_path = index_root / entry.get("snapshot", "snapshot/%s/SKILL.md" % name)
    live_path = skills_root / name / "SKILL.md"

    if not snapshot_path.is_file():
        return [Problem("no-snapshot", name, "快照不存在：%s" % snapshot_path)]
    if not live_path.is_file():
        return [Problem("no-skill-file", name, "live 文件不存在：%s" % live_path)]

    try:
        snap_lines, snap_idx = split_frontmatter(snapshot_path.read_bytes().decode("utf-8"))
        live_lines, live_idx = split_frontmatter(live_path.read_bytes().decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return [Problem("bad-frontmatter", name, str(exc))]

    live_desc = description_of(live_lines, live_idx)

    # 1. description 与词表里 approved 的定稿一致
    if mode in ("all", "desc"):
        approved = entry.get("approved")
        if not approved:
            problems.append(Problem("approved-missing", name, "词表里没有 approved description，无从判定是否偏离"))
        elif not live_desc:
            problems.append(Problem("description-empty", name, "description 是空的"))
        elif live_desc != approved:
            problems.append(Problem("description-drifted", name,
                                    "live description 与 approved 不一致（live %d 字符 / approved %d 字符）"
                                    % (len(live_desc), len(approved))))

    # 2. 词与长度
    if mode in ("all", "terms"):
        missing = [term for term in entry.get("required_terms", []) if term not in live_desc]
        if missing:
            problems.append(Problem("missing-term", name,
                                    "缺期望词 " + "、".join("「%s」" % term for term in missing)))
        limit = entry.get("max_chars", 240)
        if len(live_desc) > limit:
            problems.append(Problem("too-long", name, "description %d 字符，超过上限 %d" % (len(live_desc), limit)))

    # 3. 正文逐字节一致（description 那一行除外）
    if mode in ("all", "body"):
        snap_body = body_of(snap_lines, snap_idx)
        live_body = body_of(live_lines, live_idx)
        if [text for _, text in snap_body] != [text for _, text in live_body]:
            diffs = [(live_body[i][0], snap_body[i][1], live_body[i][1])
                     for i in range(min(len(snap_body), len(live_body)))
                     if snap_body[i][1] != live_body[i][1]]
            if len(snap_body) != len(live_body):
                shorter = min(len(snap_body), len(live_body))
                extra = len(live_body) - len(snap_body)
                diffs.append((shorter + 2, "", "多出 %d 行" % extra if extra > 0 else "少了 %d 行" % -extra))
            for line_no, _, _ in diffs[:MAX_DIFF_LINES]:
                problems.append(Problem("body-changed", name, "内容与快照不同", line=line_no))
            if len(diffs) > MAX_DIFF_LINES:
                problems.append(Problem("body-changed", name,
                                        "还有 %d 行不同，已省略" % (len(diffs) - MAX_DIFF_LINES)))
    return problems


def main(argv: list[str] | None = None) -> int:
    _utf8_streams()
    parser = argparse.ArgumentParser(description="校验接入 skill 的 description 与正文，与仓库词表/快照一致")
    parser.add_argument("--index-root", type=Path, default=DEFAULT_INDEX_ROOT,
                        help="期望词表与快照所在目录（默认 %s）" % DEFAULT_INDEX_ROOT)
    parser.add_argument("--skills-root", type=Path, default=Path.home() / ".agents" / "skills",
                        help="live skill 根目录（默认 ~/.agents/skills）")
    parser.add_argument("--routing-dir", type=Path, default=DEFAULT_ROUTING_DIR,
                        help="路由层目录，用于读 installed-skills.md（默认本脚本的上级目录）")
    parser.add_argument("--only", choices=MODES, default="all",
                        help="只查其中一类；desc 查 description 是否偏离 approved，body 用于按快照还原后确认正文没被顺手改坏")
    parser.add_argument("--json", action="store_true", help="输出 JSON，便于别的脚本吃")
    args = parser.parse_args(argv)

    index_root: Path = args.index_root.resolve()
    skills_root: Path = args.skills_root.expanduser().resolve()
    routing_dir: Path = args.routing_dir.resolve()

    expected = load_expected(index_root)
    skills = expected.get("skills", [])
    for entry in skills:
        entry.setdefault("max_chars", expected.get("max_chars", 240))

    problems = check_scope(expected, routing_dir)
    for entry in skills:
        problems += check_skill(entry, skills_root, index_root, args.only)

    ok = not problems
    if args.json:
        print(json.dumps({
            "ok": ok,
            "mode": args.only,
            "skill_count": len(skills),
            "index_root": str(index_root),
            "skills_root": str(skills_root),
            "problems": [problem.as_dict() for problem in problems],
        }, ensure_ascii=False, indent=2))
    else:
        for problem in problems:
            print(problem.render())
        if ok:
            print("%d 个 skill 全部符合期望（mode=%s）" % (len(skills), args.only))
        else:
            print("发现 %d 处问题（mode=%s，skill 共 %d 个）" % (len(problems), args.only, len(skills)))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
