#!/usr/bin/env python3
"""回答一个问题：借来的那 9 个技能，上游那份被人更新过没有。

为什么要有它：这 9 个技能是从 `D:\\agent-skills\\installed\\` 拷进仓库的，拷完就不再跟随上游。
上游修了 bug 不会自己来，我们也不知道自己落后了——除非有人主动比对。这个脚本把比对变成一条命令。

它**不判失败**：上游变了不是我们的错，退出码保持 0。它只负责把「哪几个技能、哪几个文件、
基线里的 sha256 与现在差在哪」说清楚，要不要合并由人决定（PRD-008 §7 D3 仍是人工判断）。

顺带报一件相关的事：仓库里那份副本还等于基线吗。若不等，说明我们本地改过，
这时不该拿上游整份覆盖——两边的差异都得看。

用法：

    python .agents/skills/skill-routing/scripts/check_upstream_drift.py
    python .../check_upstream_drift.py --json
    python .../check_upstream_drift.py --baseline <基线文件> --upstream-root <上游根> --skills-root <仓库 skills 目录>

退出码：0 比对完成（无论有没有漂移）；2 基线缺失或读不动、根目录不对。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_BASELINE = REPO_ROOT / "skills" / ".upstream-baseline.json"
DEFAULT_SKILLS_ROOT = REPO_ROOT / "skills"
MAX_LISTED = 10


def _utf8_streams() -> None:
    """Windows 控制台默认按本地代码页编码，中文输出会变乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_of(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256_of(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def compare(name: str, expected: list[dict], upstream_dir: Path, copy_dir: Path) -> dict:
    base = {item["path"]: item["sha256"] for item in expected}
    row = {"skill": name, "upstream_dir": str(upstream_dir), "drift": False, "findings": []}

    if not upstream_dir.is_dir():
        row["drift"] = True
        row["findings"].append({"kind": "upstream-missing", "detail": str(upstream_dir)})
    else:
        now = tree_of(upstream_dir)
        removed = sorted(set(base) - set(now))
        added = sorted(set(now) - set(base))
        changed = sorted(path for path in set(base) & set(now) if base[path] != now[path])
        if removed:
            row["drift"] = True
            row["findings"].append({"kind": "upstream-removed", "paths": removed[:MAX_LISTED],
                                    "count": len(removed)})
        if added:
            row["drift"] = True
            row["findings"].append({"kind": "upstream-added", "paths": added[:MAX_LISTED],
                                    "count": len(added)})
        if changed:
            row["drift"] = True
            row["findings"].append({"kind": "upstream-changed", "paths": changed[:MAX_LISTED],
                                    "count": len(changed)})

    if not copy_dir.is_dir():
        row["copy"] = "missing"
    elif tree_of(copy_dir) == base:
        row["copy"] = "matches-baseline"
    else:
        row["copy"] = "differs-from-baseline"
    return row


def render(rows: list[dict], baseline: dict, baseline_path: Path) -> None:
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    drifted = [row for row in rows if row["drift"]]
    print("基线：%s（记录于 %s，%d 个技能）"
          % (baseline_path, baseline.get("recorded_at", "?"), len(rows)))
    print("检查时间：%s" % checked_at)
    for row in rows:
        if not row["drift"]:
            continue
        print("[漂移] %s  上游目录 %s" % (row["skill"], row["upstream_dir"]))
        for finding in row["findings"]:
            if finding["kind"] == "upstream-missing":
                print("    上游目录不存在：%s" % finding["detail"])
            else:
                kinds = {"upstream-changed": "内容变了", "upstream-added": "多了文件",
                         "upstream-removed": "少了文件"}
                print("    %s %d 个：%s" % (kinds[finding["kind"]], finding["count"],
                                            "、".join(finding["paths"])))
    if drifted:
        print("上游有变化的 %d 个：%s" % (len(drifted), "、".join(row["skill"] for row in drifted)))
        print("要不要同步是人工决定：整份覆盖会连带覆盖我们对仓库副本的改动，先看下面这一行。")
    else:
        print("上游 9 个技能与基线一致，没有需要处理的变化。")
    off = [row for row in rows if row["copy"] != "matches-baseline"]
    if off:
        for row in off:
            print("[副本] %s 仓库副本 %s（基线是迁移时的样子）" % (row["skill"], row["copy"]))


def main(argv: list[str] | None = None) -> int:
    _utf8_streams()
    parser = argparse.ArgumentParser(description="比对借来的技能与上游现状，只报告不判失败")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE,
                        help="基线 JSON（默认 <仓库>/skills/.upstream-baseline.json）")
    parser.add_argument("--upstream-root", type=Path, default=None,
                        help="上游根目录（默认取基线里记的 upstream_root）")
    parser.add_argument("--skills-root", type=Path, default=DEFAULT_SKILLS_ROOT,
                        help="仓库 skills 目录，用于顺带核对仓库副本（默认 <仓库>/skills）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    baseline_path = args.baseline.resolve()
    if not baseline_path.is_file():
        print("用法错误：找不到基线 %s" % baseline_path)
        return 2
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        skills = baseline["skills"]
    except (json.JSONDecodeError, KeyError) as exc:
        print("用法错误：基线读不动（%s）" % exc)
        return 2
    if not isinstance(skills, dict) or not skills:
        print("用法错误：基线里没有 skills")
        return 2

    upstream_root = (args.upstream_root or Path(
        baseline.get("upstream_root", "D:/agent-skills/installed"))).resolve()
    skills_root = args.skills_root.resolve()
    rows = [compare(name, entry["files"], upstream_root / name, skills_root / name)
            for name, entry in sorted(skills.items())]

    if args.json:
        print(json.dumps({"baseline": str(baseline_path), "upstream_root": str(upstream_root),
                          "skills_root": str(skills_root),
                          "drifted": [row["skill"] for row in rows if row["drift"]],
                          "skills": rows}, ensure_ascii=False, indent=2))
    else:
        render(rows, baseline, baseline_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
