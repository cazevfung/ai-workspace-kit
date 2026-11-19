#!/usr/bin/env python3
"""把 `~/.agents/skills/` 下的 skill 改成指向仓库某份检出的 junction，并报出每个链接的 now-state。

为什么需要它：迁移之后 `~/.agents/skills/<名字>` 不再是自己的一堆文件，而是一个目录联结
（junction）。合入 master 之后这些联结必须从工作树重指到主检出——手敲 18 条 `mklink /J`
必然漏一条，而漏掉的那条**不会报错**（PRD-007 实测：联结悬空时 Cindy 静默跳过，`mklink`
退出码和索引检查都看不出区别）。所以重指变成一条命令，且必须能重复跑。

用法：

    python .agents/skills/skill-routing/scripts/relink_skills.py --source-root {{WORKSPACE}}/skills
    python .../relink_skills.py --source-root {{WORKSPACE}}/.worktrees/AW-039/skills --dry-run
    python .../relink_skills.py --source-root ... --names brainstorming,writing-plans
    python .../relink_skills.py --source-root ... --json

行为：

  * 名字默认取「源根下含 `SKILL.md` 的目录」，`--names` 可以只对其中一部分动手。
  * 每个名字按现状分三种动作：`created`（原来没有，建联结）、`relinked`（原来指着别处，删链接再建）、
    `unchanged`（已经指着这里，什么都不做）。这三种之外一律报错退出，不自作主张。
  * 删旧链接只用 `rmdir`（Windows 上只删联结本身，不动目标内容）。**真实目录绝不动**——
    遇到不是联结的目录就拒绝并提示先备份，宁可失败也不删文件。
  * 每次动作后回读一遍 `Target` 并核对，只看 `mklink` 退出码是不够的。

退出码：0 全部符合期望；1 有名字处理失败；2 用法错误。
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

DEFAULT_LIVE_ROOT = Path.home() / ".agents" / "skills"
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _utf8_streams() -> None:
    """Windows 控制台默认按本地代码页编码，中文输出会变乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def read_target(path: Path) -> str | None:
    """读联结目标。读不到（普通目录/文件）返回 None。

    Windows 上 `os.readlink` 会带上 `\\\\?\\` 前缀，这里去掉，方便人读与比较。
    """
    try:
        raw = os.readlink(path)
    except (OSError, ValueError):
        return None
    if raw.startswith("\\\\?\\UNC\\"):
        return "\\\\" + raw[len("\\\\?\\UNC\\"):]
    if raw.startswith("\\\\?\\"):
        return raw[len("\\\\?\\"):]
    return raw


def is_junction(path: Path) -> bool:
    try:
        attrs = os.lstat(path).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attrs & REPARSE_POINT)


def state_of(path: Path) -> dict:
    """一个 live 路径的现状：missing / junction / directory / file。"""
    if not path.exists() and not is_junction(path):
        return {"kind": "missing", "target": None}
    if is_junction(path):
        return {"kind": "junction", "target": read_target(path)}
    return {"kind": "directory" if path.is_dir() else "file", "target": None}


def same_path(link_target: str | None, wanted: Path) -> bool:
    if not link_target:
        return False
    return os.path.normcase(os.path.realpath(link_target)) == os.path.normcase(
        os.path.realpath(wanted))


def make_junction(link: Path, target: Path) -> tuple[bool, str]:
    """建 junction，返回（成功, 说明）。不信退出码，建完回读核对。"""
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not is_junction(link):
        return False, "mklink 失败：%s%s" % (proc.stdout.strip(), proc.stderr.strip())
    got = read_target(link)
    if not same_path(got, target):
        return False, "建好了但 Target 是 %s，不是 %s" % (got, target)
    return True, got or ""


def drop_junction(link: Path) -> tuple[bool, str]:
    """删联结本身，不碰目标。只对确认是联结的路径调用。"""
    try:
        os.rmdir(link)
    except OSError as exc:
        return False, "删旧联结失败：%s" % exc
    if is_junction(link):
        return False, "删完仍然是联结"
    return True, ""


def skill_names(source_root: Path, wanted: list[str] | None) -> list[str]:
    if wanted:
        names = [name.strip() for name in wanted if name.strip()]
    else:
        names = sorted(
            entry.name for entry in source_root.iterdir()
            if entry.is_dir() and (entry / "SKILL.md").is_file()
        )
    for name in names:
        if not (source_root / name / "SKILL.md").is_file():
            raise SystemExit("用法错误：%s 不是源根下的 skill 目录（缺 SKILL.md）"
                             % (source_root / name))
    return names


def relink_one(name: str, source_root: Path, live_root: Path, dry_run: bool) -> dict:
    src = source_root / name
    live = live_root / name
    state = state_of(live)
    row = {"name": name, "target": src.as_posix(), "was": state["target"], "was_kind": state["kind"]}

    if state["kind"] == "missing":
        row["action"] = "created"
    elif state["kind"] == "junction":
        row["action"] = "unchanged" if same_path(state["target"], src) else "relinked"
    else:
        row["action"] = "refused"
        row["error"] = ("live 路径是真实%s，不是联结。先备份再删，本脚本不替你删文件"
                        % ("目录" if state["kind"] == "directory" else "文件"))
        return row

    if row["action"] == "unchanged":
        row["target"] = state["target"] or row["target"]
        row["ok"] = True
        return row
    if dry_run:
        row["ok"] = True
        row["action"] = "would-" + row["action"]
        return row

    if row["action"] == "relinked":
        ok, note = drop_junction(live)
        if not ok:
            row["ok"] = False
            row["error"] = note
            return row
    ok, note = make_junction(live, src)
    row["ok"] = ok
    row["target"] = note if ok else row["target"]
    if not ok:
        row["error"] = note
    return row


def main(argv: list[str] | None = None) -> int:
    _utf8_streams()
    parser = argparse.ArgumentParser(
        description="把 ~/.agents/skills 下的 skill 重指到仓库的某份检出（幂等）")
    parser.add_argument("--source-root", type=Path, required=True,
                        help="要指向的检出里的 skills 目录，例如 {{WORKSPACE}}/skills")
    parser.add_argument("--live-root", type=Path, default=DEFAULT_LIVE_ROOT,
                        help="live skill 根目录（默认 ~/.agents/skills）")
    parser.add_argument("--names", default=None,
                        help="只处理这些名字，逗号分隔；默认取源根下所有含 SKILL.md 的目录")
    parser.add_argument("--dry-run", action="store_true", help="只报告打算做什么，不改文件系统")
    parser.add_argument("--json", action="store_true", help="输出 JSON，便于别的脚本吃")
    args = parser.parse_args(argv)

    source_root = args.source_root.expanduser().resolve()
    live_root = args.live_root.expanduser().resolve()
    if not source_root.is_dir():
        print("用法错误：源根不存在：%s" % source_root)
        return 2
    if not live_root.is_dir():
        print("用法错误：live 根不存在：%s" % live_root)
        return 2

    names = skill_names(source_root, args.names.split(",") if args.names else None)
    rows = [relink_one(name, source_root, live_root, args.dry_run) for name in names]
    failed = [row for row in rows if not row.get("ok")]

    if args.json:
        print(json.dumps({"ok": not failed, "source_root": str(source_root),
                          "live_root": str(live_root), "dry_run": args.dry_run,
                          "rows": rows}, ensure_ascii=False, indent=2))
    else:
        print("%-34s %-9s %-9s %s" % ("skill", "was", "action", "Target"))
        for row in rows:
            print("%-34s %-9s %-9s %s" % (row["name"], row["was_kind"], row["action"],
                                          row.get("target", "")))
        counts = {}
        for row in rows:
            counts[row["action"]] = counts.get(row["action"], 0) + 1
        print("共 %d 个：%s" % (len(rows), "，".join(
            "%s %d" % (key, value) for key, value in sorted(counts.items()))))
        for row in failed:
            print("[失败] %s：%s" % (row["name"], row.get("error", "")))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
