#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 工作区套件 · 卸载。

只删掉本套件装进去的东西，不动用户原有内容。依据是安装时写的清单
`~/.agents/.ai-workspace-kit.json`：只有清单里记过的技能才会被移除。

    python uninstall.py                         # 删工作区（默认 ~/ai-workspace，会二次确认）
    python uninstall.py --skills                # 只移除本套件装的技能
    python uninstall.py --workspace D:/ai-ws --yes
    python uninstall.py --all                   # 连 ~/.agents/center.json 与 intake/ 一起删
    python uninstall.py --purge-data            # 连工作区的 .data/ 进度库一起删
    python uninstall.py --dry-run

不做的事：不删你自己的工作区数据（除非 --purge-data），不删不在清单里的技能。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

MANIFEST_NAME = ".ai-workspace-kit.json"


def say(m=""):
    print(m, flush=True)


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(question + " [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="卸载 AI 工作区套件")
    parser.add_argument("--workspace", default=None, help="工作区目录（默认从安装清单读取）")
    parser.add_argument("--agents-home", default=str(Path.home() / ".agents"),
                        help="用户级目录（默认 ~/.agents）")
    parser.add_argument("--skills", action="store_true", help="只移除本套件装的技能")
    parser.add_argument("--all", action="store_true", help="连 center.json 与 intake/ 一起删")
    parser.add_argument("--purge-data", action="store_true", help="连工作区的 .data/ 一起删")
    parser.add_argument("--yes", action="store_true", help="不问，直接做")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划")
    args = parser.parse_args(argv)

    manifest = {}
    agents_home = Path(args.agents_home).expanduser().resolve()
    manifest_path = agents_home / MANIFEST_NAME
    center = agents_home / "center.json"
    intake = agents_home / "intake"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            say(f"清单读不出来，按空清单处理：{manifest_path}")

    workspace = Path(args.workspace or manifest.get("workspace") or (Path.home() / "ai-workspace")).expanduser()
    skills_dir = Path(manifest.get("skills_dir") or (agents_home / "skills"))
    installed = set(manifest.get("installed_skills") or [])

    say(f"工作区：{workspace}")
    say(f"技能目录：{skills_dir}")

    if not args.skills:
        if workspace.exists():
            if not confirm(f"删除工作区 {workspace}？", args.yes):
                say("  跳过工作区")
            elif args.dry_run:
                say("  预演：删除工作区")
            else:
                if args.purge_data:
                    shutil.rmtree(workspace)
                    say("  已删除工作区（含 .data/）")
                else:
                    for child in workspace.iterdir():
                        if child.name == ".data":
                            continue
                        if child.is_dir():
                            shutil.rmtree(child)
                        else:
                            child.unlink()
                    say("  已清空工作区（保留 .data/；要连进度库一起删加 --purge-data）")
        else:
            say("  工作区不存在，跳过")

    if installed:
        removed = 0
        for name in sorted(installed):
            target = skills_dir / name
            if target.exists():
                if not args.dry_run:
                    shutil.rmtree(target)
                removed += 1
        say(f"  移除技能 {removed} 个（清单内）")
    elif not args.skills:
        say("  清单里没有技能记录，未动技能目录")

    if args.all:
        if center.is_file():
            if not args.dry_run:
                center.unlink()
            say("  已删 center.json")
        if intake.exists():
            if not args.dry_run:
                shutil.rmtree(intake)
            say("  已删 intake/")
        # 安装时写在工作区里的那份指针（记着这次用的是哪个 agents-home）：
        # 用户级配置都删了，留着它只会让闸门报一个已经不该存在的目录。
        pointer = workspace / ".agents" / "center.json"
        if pointer.is_file():
            if not args.dry_run:
                pointer.unlink()
            say(f"  已删工作区指针 {pointer}")

    if manifest_path.is_file() and not args.dry_run:
        manifest_path.unlink()

    say("\n卸载完成。" + ("（预演，未改动任何文件）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
