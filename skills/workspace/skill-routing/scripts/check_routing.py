#!/usr/bin/env python3
"""反向校验节点级 skill 路由层。

它只回答四件事，答不上来就非零退出并指出 `文件:行号`：

  1. unknown-skill           归口或处置里点了一个磁盘上不存在的 skill
  2. missing-owner           某个节点没有归口
  3. undispositioned-skill   装了的 skill 既没接入节点，也没写明不接
  4. unknown-node            角色表（或处置表、缺口表）引用了不存在的节点编号

顺带查三件结构性的事：节点是否齐 N01–N30、三张角色表是否覆盖全部节点、缺口清单是否覆盖所有无 skill 归口的节点。

用法：

    python .agents/skills/skill-routing/scripts/check_routing.py
    python .agents/skills/skill-routing/scripts/check_routing.py --json
    python .../check_routing.py --root <路由层目录> --installed-root <skill 根> [--installed-root ...]

退出码：0 全部通过；1 发现问题；2 用法错误。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

NODE_COUNT = 30
NODE_ID = re.compile(r"^N\d{2}$")
OWNER = re.compile(r"^(skill|action):(.+)$")
EXPECTED = ["N%02d" % i for i in range(1, NODE_COUNT + 1)]
SEPARATOR_CHARS = set("|-: ")
CONNECTOR = " + "


def _utf8_streams() -> None:
    """Windows 控制台默认按本地代码页编码，中文输出会变乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


class Problem:
    def __init__(self, kind: str, path: Path, line: int, message: str) -> None:
        self.kind = kind
        self.path = path
        self.line = line
        self.message = message

    def render(self) -> str:
        return "%s:%d: [%s] %s" % (self.path, self.line, self.kind, self.message)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "path": str(self.path), "line": self.line, "message": self.message}


def cells_of(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(set(cell) <= SEPARATOR_CHARS and cell for cell in cells)


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def frontmatter_name(skill_md: Path) -> str | None:
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    for raw in text[3:end].splitlines():
        match = re.match(r"^name:\s*(.+?)\s*$", raw.strip())
        if match:
            return match.group(1).strip("\"'")
    return None


def installed_skills(roots: list[Path], manifest: Path) -> dict[str, list[str]]:
    """名字 -> 来源说明。磁盘扫描 + 托管清单取并集。"""
    found: dict[str, list[str]] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            skill_md = entry / "SKILL.md"
            if not (entry.is_dir() and skill_md.is_file()):
                continue
            name = frontmatter_name(skill_md) or entry.name
            found.setdefault(name, []).append(str(root))
    if manifest.is_file():
        for raw in read_lines(manifest):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = re.split(r"\t+|\s{2,}", line, maxsplit=1)
            found.setdefault(parts[0].strip(), []).append(
                parts[1].strip() if len(parts) > 1 else str(manifest)
            )
    return found


def owner_entries(cell: str, path: Path, line: int, problems: list[Problem]) -> list[str]:
    """归口单元格 -> ['skill:x', 'action:y']；不合格直接记错误。"""
    entries = []
    for part in cell.replace("`", "").split(CONNECTOR):
        part = part.strip()
        if not part:
            continue
        match = OWNER.match(part)
        if not match:
            problems.append(Problem("malformed-row", path, line,
                                    "归口 %r 没有 skill:/action: 前缀，脚本无法判定它的形态" % part))
            continue
        kind, value = match.group(1), match.group(2).strip()
        if not value:
            problems.append(Problem("missing-owner", path, line, "归口 %r 前缀后面是空的" % part))
            continue
        entries.append("%s:%s" % (kind, value))
    return entries


def parse_nodes(path: Path, problems: list[Problem]) -> dict[str, dict]:
    """编号 -> {line, owners, fallback}。"""
    nodes: dict[str, dict] = {}
    if not path.is_file():
        problems.append(Problem("missing-file", path, 0, "总表文件不存在"))
        return nodes
    for lineno, raw in enumerate(read_lines(path), 1):
        cells = cells_of(raw)
        if not cells or not NODE_ID.match(cells[0]) or is_separator(cells):
            continue
        if len(cells) != 5:
            problems.append(Problem("malformed-row", path, lineno,
                                    "节点行应有 5 列（编号/节点/归口/状态/兜底动作），实际 %d 列" % len(cells)))
            continue
        node_id, title, owner_cell, status, fallback = cells
        if node_id in nodes:
            problems.append(Problem("duplicate-node", path, lineno, "节点编号 %s 重复出现" % node_id))
            continue
        owners = owner_entries(owner_cell, path, lineno, problems)
        if not owners:
            problems.append(Problem("missing-owner", path, lineno, "节点 %s（%s）没有归口" % (node_id, title)))
        if not fallback:
            problems.append(Problem("missing-fallback", path, lineno, "节点 %s 没有兜底动作" % node_id))
        nodes[node_id] = {"line": lineno, "title": title, "owners": owners,
                          "status": status, "fallback": fallback}
    return nodes


def section_rows(path: Path, keywords: dict[str, str]) -> dict[str, list[tuple[int, list[str]]]]:
    """按二级标题归类的表行。keywords: 关键片段 -> section key。"""
    buckets: dict[str, list[tuple[int, list[str]]]] = {key: [] for key in keywords.values()}
    current = None
    if not path.is_file():
        return buckets
    for lineno, raw in enumerate(read_lines(path), 1):
        text = raw.strip()
        if not text:
            continue
        if text.startswith("#"):
            current = None
            for needle, key in keywords.items():
                if needle in text:
                    current = key
                    break
            continue
        if current is None or not text.startswith("|"):
            continue
        cells = cells_of(text)
        if is_separator(cells):
            continue
        buckets[current].append((lineno, cells))
    return buckets


def node_refs(cell: str) -> list[str]:
    return [token for token in re.split(r"[\s,、/]+", cell.strip()) if token]


def check_nodes(nodes: dict[str, dict], path: Path, problems: list[Problem]) -> None:
    missing = [n for n in EXPECTED if n not in nodes]
    extra = sorted(set(nodes) - set(EXPECTED))
    if missing:
        problems.append(Problem("orphan-node", path, 0,
                                "总表缺这些节点编号：%s（应从 N01 连续到 N30）" % " ".join(missing)))
    if extra:
        problems.append(Problem("orphan-node", path, 0,
                                "总表出现不该有的编号：%s" % " ".join(extra)))
    for node_id, row in nodes.items():
        for entry in row["owners"]:
            if entry.startswith("skill:") and entry[6:] not in KNOWN_SKILLS:
                problems.append(Problem("unknown-skill", path, row["line"],
                                        "节点 %s 的归口点了磁盘上不存在的 skill：%s" % (node_id, entry[6:])))


def check_roles(roles_path: Path, nodes: dict[str, dict], problems: list[Problem]) -> dict[str, set[str]]:
    keywords = {"协调": "协调", "执行": "执行", "验收": "验收"}
    buckets = section_rows(roles_path, keywords)
    if not roles_path.is_file():
        problems.append(Problem("missing-file", roles_path, 0, "角色入口文件不存在"))
        return {}
    covered: dict[str, set[str]] = {}
    for role, rows in buckets.items():
        ids: set[str] = set()
        if not rows:
            problems.append(Problem("orphan-role", roles_path, 0, "没有找到「%s会话」那张表" % role))
            continue
        for lineno, cells in rows:
            if not NODE_ID.match(cells[0]):
                continue
            node_id = cells[0]
            if node_id not in nodes:
                problems.append(Problem("unknown-node", roles_path, lineno,
                                        "%s会话表引用了不存在的节点 %s" % (role, node_id)))
                continue
            ids.add(node_id)
        covered[role] = ids
    if len(covered) == 3:
        union = set().union(*covered.values())
        not_covered = [n for n in EXPECTED if n not in union]
        if not_covered:
            problems.append(Problem("uncovered-node", roles_path, 0,
                                    "三张角色表合起来没有覆盖：%s" % " ".join(not_covered)))
    return covered


def check_dispositions(path: Path, nodes: dict[str, dict], installed: dict[str, list[str]],
                       problems: list[Problem]) -> None:
    buckets = section_rows(path, {"处置表": "处置"})
    if not path.is_file():
        problems.append(Problem("missing-file", path, 0, "已装 skill 处置表不存在"))
        return
    handled: set[str] = set()
    for lineno, cells in buckets.get("处置", []):
        if cells[0] in {"已装 skill", ""}:
            continue
        if len(cells) < 4:
            problems.append(Problem("malformed-row", path, lineno,
                                    "处置行应有 4 列（skill/来源/处置/节点或项目面）"))
            continue
        name, _source, verdict, target = cells[0], cells[1], cells[2], cells[3]
        handled.add(name)
        if verdict.startswith("接入"):
            refs = node_refs(target)
            if not refs:
                problems.append(Problem("missing-owner", path, lineno,
                                        "%s 写着接入，但没写接到哪个节点" % name))
            for ref in refs:
                if not NODE_ID.match(ref):
                    problems.append(Problem("unknown-node", path, lineno,
                                            "%s 的节点列出现了非节点编号 %r" % (name, ref)))
                elif ref not in nodes:
                    problems.append(Problem("unknown-node", path, lineno,
                                            "%s 接入了不存在的节点 %s" % (name, ref)))
        elif verdict.startswith("明确不接"):
            if not target.strip():
                problems.append(Problem("missing-owner", path, lineno,
                                        "%s 写着明确不接，但没有写它属于哪个项目面" % name))
        else:
            problems.append(Problem("malformed-row", path, lineno,
                                    "%s 的处置 %r 既不是「接入」也不是「明确不接」" % (name, verdict)))
    for name in sorted(installed):
        if name not in handled:
            problems.append(Problem("undispositioned-skill", path, 0,
                                    "已装 skill %s 没有处置（来源：%s）；要么接入某个节点，要么列入明确不接并写明项目面"
                                    % (name, "；".join(installed[name]))))


def check_gaps(path: Path, nodes: dict[str, dict], problems: list[Problem]) -> None:
    buckets = section_rows(path, {"缺口节点": "缺口"})
    if not path.is_file():
        problems.append(Problem("missing-file", path, 0, "缺口清单不存在"))
        return
    listed: set[str] = set()
    for lineno, cells in buckets.get("缺口", []):
        if not NODE_ID.match(cells[0]):
            continue
        node_id = cells[0]
        listed.add(node_id)
        if node_id not in nodes:
            problems.append(Problem("unknown-node", path, lineno, "缺口清单列了不存在的节点 %s" % node_id))
            continue
        if any(entry.startswith("skill:") for entry in nodes[node_id]["owners"]):
            problems.append(Problem("malformed-row", path, lineno,
                                    "%s 已经有 skill 归口，不该出现在缺口清单里" % node_id))
        if len(cells) < 3 or not cells[1] or not cells[2]:
            problems.append(Problem("missing-fallback", path, lineno,
                                    "%s 的缺口行缺兜底动作或「不新建 skill 的理由」" % node_id))
    for node_id, row in nodes.items():
        if any(entry.startswith("skill:") for entry in row["owners"]):
            continue
        if node_id not in listed:
            problems.append(Problem("missing-gap", path, 0,
                                    "节点 %s 没有 skill 归口，却没进缺口清单" % node_id))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验节点级 skill 路由层")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent,
                        help="路由层目录（默认：本脚本的上级目录）")
    parser.add_argument("--installed-root", type=Path, action="append", default=None,
                        help="已装 skill 根目录，可重复；给了就替代默认的两处")
    parser.add_argument("--manifest", type=Path, default=None,
                        help="托管 skill 名单（默认 <root>/scripts/installed-manifest.txt）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)
    _utf8_streams()

    root = args.root.resolve()
    if not root.is_dir():
        print("check_routing: 路由层目录不存在：%s" % root, file=sys.stderr)
        return 2

    installed_roots = args.installed_root or [root.parent, Path.home() / ".agents" / "skills"]
    installed_roots = [Path(p).expanduser() for p in installed_roots]
    manifest = args.manifest or (root / "scripts" / "installed-manifest.txt")
    global KNOWN_SKILLS
    KNOWN_SKILLS = set(installed_skills(installed_roots, manifest))

    problems: list[Problem] = []
    for installed_root in installed_roots:
        if not installed_root.is_dir():
            problems.append(Problem("missing-root", installed_root, 0,
                                    "已装 skill 根目录不存在，归口里的 skill 名会全部被判为不存在；用 --installed-root 指定实际位置"))
    nodes = parse_nodes(root / "nodes.md", problems)
    check_nodes(nodes, root / "nodes.md", problems)
    covered = check_roles(root / "roles.md", nodes, problems)
    check_dispositions(root / "installed-skills.md", nodes,
                       installed_skills(installed_roots, manifest), problems)
    check_gaps(root / "gaps.md", nodes, problems)

    union = set().union(*covered.values()) if covered else set()
    coverage_line = "角色表覆盖：%s；三张合并覆盖 %d/%d" % (
        " / ".join("%s %d 个" % (role, len(ids)) for role, ids in covered.items()) or "无",
        len(union & set(EXPECTED)), NODE_COUNT)

    if args.json:
        print(json.dumps({"ok": not problems, "skill_count": len(KNOWN_SKILLS),
                          "node_count": len(nodes),
                          "roles": {role: sorted(ids) for role, ids in covered.items()},
                          "covered": sorted(union & set(EXPECTED)),
                          "problems": [p.as_dict() for p in problems]}, ensure_ascii=False, indent=2))
    else:
        for problem in problems:
            print(problem.render())
        print("\n" + coverage_line)
        print("%d 个节点 / %d 个已装 skill / %d 个问题" % (len(nodes), len(KNOWN_SKILLS), len(problems)))
    return 0 if not problems else 1


KNOWN_SKILLS: set[str] = set()

if __name__ == "__main__":
    sys.exit(main())
