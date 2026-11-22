"""安装面体检：一个已登记项目「装好了没有」，一条只读命令能判红。

PRD-013 R5 的起因是「装了没接」：项目登记了、工单也开得出来，但那个仓库没装钩子，
于是「没有工单不许改文件」在那条仓库里根本不生效，而且没有任何一步会因此失败。
这里把三件事变成退出码：

1. 这个根是 git 仓库时，`core.hooksPath` 是否指向**本仓库自己的** `.githooks`；
2. `.worktrees/` 是否被 git 忽略（工作树目录不该进版本库）；
3. 仓库根是否有 `AGENTS.md` 或 `CLAUDE.md`（入口说明）。

非 git 项目只报「不是仓库，不检查钩子」，不算失败。

**这个脚本只读。** 它不改任何仓库的 `git config`、不写 `.gitignore`、不建文件：
缺项的补法写在输出里，由人决定要不要动手。理由是「体检」与「动手改别人的仓库」
是两件事，混在一起就没人敢在业务项目上跑它了。

用法（仓库根）：

    python tools/project_doctor.py                  # 查所有已登记项目
    python tools/project_doctor.py --db <库路径>     # 指定中心库
    python tools/project_doctor.py --root <目录>     # 只查这一个目录（不需要库）
    python tools/project_doctor.py --json           # 机器可读

退出码：全部成立 0；任何一项不成立 1（库里没登记项目也算「没东西可查」，不是失败）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import work_gate

REPO = Path(__file__).resolve().parent.parent

OK = "[√]"
MISSING = "[×]"
SKIPPED = "[-]"


def run_git(root, *args):
    """跑一条 git 命令并返回 CompletedProcess；git 不在或超时就返回 None。

    返回 None 与「命令失败」必须分得开：前者是判不了，后者是「确实没设」。这里都由
    调用方显式处理——没设钩子是缺项，判不了也说成缺项，因为「不知道装没装」不能当装好了。
    """
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None


def is_repository(root):
    result = run_git(root, "rev-parse", "--is-inside-work-tree")
    return bool(result is not None and result.returncode == 0 and result.stdout.strip() == "true")


def same_path(value, expected, root):
    """配置里的路径可能是相对仓库根的；按 git 的口径解析再比。"""
    path = Path(value.strip())
    if not path.is_absolute():
        path = Path(root) / path
    try:
        left = Path(os.path.normcase(str(path.resolve())))
        right = Path(os.path.normcase(str(Path(expected).resolve())))
    except OSError:
        return False
    return left == right


def inspect(root):
    """查一个目录的三项，返回结论与缺项（缺项各带一句补法）。"""
    root = Path(root).resolve()
    report = {"root": str(root), "is_git": is_repository(root), "hooks_path": None, "hooks_ok": None,
              "worktrees_ignored": None, "docs_found": None, "docs": [], "problems": [], "notes": []}
    if report["is_git"]:
        result = run_git(root, "config", "--get", "core.hooksPath")
        value = (result.stdout or "").strip() if result is not None and result.returncode == 0 else ""
        report["hooks_path"] = value or None
        expected = root / ".githooks"
        report["hooks_ok"] = bool(value) and same_path(value, expected, root)
        if not report["hooks_ok"]:
            current = f"现在是 {value}" if value else "现在没设（空）"
            report["problems"].append({
                "item": "hooks", "detail": f"core.hooksPath 没指向本仓库的 .githooks；{current}",
                "fix": f"git -C {root.as_posix()} config core.hooksPath {(expected).as_posix()}"
                       "（这条要你自己跑；本脚本不改任何仓库配置）",
            })
        # 尾部的 `/` 不是笔误：`.worktrees/` 这种「只忽略目录」的写法要靠它才认出来，
        # 而工作树目录在体检的那一刻通常还不存在，git 无从判断它是不是目录。
        ignored = run_git(root, "check-ignore", "-q", ".worktrees/")
        # check-ignore 退出 0 表示被忽略；1 是「没被忽略」，128 等是「查不出来」，都按缺项报。
        report["worktrees_ignored"] = bool(ignored is not None and ignored.returncode == 0)
        if not report["worktrees_ignored"]:
            report["problems"].append({
                "item": "worktrees", "detail": ".worktrees/ 没有被 git 忽略",
                "fix": f"把一行 `.worktrees/` 加进 {root.as_posix()}/.gitignore 并提交它"
                       "（工作树目录不该进版本库）",
            })
    else:
        report["notes"].append("钩子与 .worktrees 的忽略状态都是 git 概念，这里不检查")
    report["docs"] = [name for name in ("AGENTS.md", "CLAUDE.md") if (root / name).is_file()]
    report["docs_found"] = bool(report["docs"])
    if not report["docs_found"]:
        report["problems"].append({
            "item": "docs", "detail": "仓库根既没有 AGENTS.md 也没有 CLAUDE.md",
            "fix": f"在 {root.as_posix()} 写一份 AGENTS.md（或 CLAUDE.md）：入口说明、"
                   "目录约定、开工前该读哪份 PRD",
        })
    return report


def registered_roots(db_path):
    """已登记项目的 (project_id, root)；读不出来就抛，不猜。"""
    center = work_gate.load_center()(db_path)
    listed = center.execute("list_projects", {})
    if not listed["ok"]:
        raise work_gate.CenterUnreadable(listed["error"]["code"], listed["error"]["message"])
    return [(item["project_id"], item["root"]) for item in listed["result"]["items"]]


def render(report):
    """一个项目一块：三项各一行，缺项后面紧跟补法。"""
    lines = [f"{report['root']}（{report.get('project_id', '-')}）"]
    if report["is_git"]:
        if report["hooks_ok"]:
            lines.append(f"  {OK} 钩子：core.hooksPath = {report['hooks_path']}")
        else:
            lines.append(f"  {MISSING} 钩子：{report['hooks_path'] or 'core.hooksPath 未设置'}")
        lines.append(f"  {OK if report['worktrees_ignored'] else MISSING} .worktrees 已忽略")
    else:
        lines.append(f"  {SKIPPED} 不是仓库，不检查钩子")
    if report["docs_found"]:
        lines.append(f"  {OK} 文档：{'、'.join(report['docs'])}")
    else:
        lines.append(f"  {MISSING} 文档：没有 AGENTS.md / CLAUDE.md")
    for problem in report["problems"]:
        lines.append(f"      补法：{problem['fix']}")
    for note in report["notes"]:
        lines.append(f"  {SKIPPED} {note}")
    return lines


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="安装面体检：钩子、工作树忽略、入口文档")
    parser.add_argument("--db", default=None, help="中心库路径；不给就按 TASK_CENTER_DB / center.json 找")
    parser.add_argument("--root", default=None, help="只查这一个目录，不需要中心库")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv[1:])

    reports = []
    if args.root:
        if not Path(args.root).is_dir():
            print(f"目录不存在：{args.root}", file=sys.stderr)
            return 1
        report = inspect(args.root)
        report["project_id"] = "-"
        reports.append(report)
    else:
        db_path = work_gate.find_db(args.db)
        if db_path is None or not Path(db_path).is_file():
            print("中心库没找到：给 --db，或检查 ~/.agents/center.json 的 db 字段", file=sys.stderr)
            return 1
        try:
            projects = registered_roots(db_path)
        except work_gate.CenterUnreadable as error:
            print(f"读库失败：{error.code}（{db_path}）", file=sys.stderr)
            print(f"  {error.message}", file=sys.stderr)
            return 1
        for project_id, root in projects:
            if not Path(root).is_dir():
                report = {"root": str(root), "project_id": project_id, "is_git": False,
                          "hooks_path": None, "hooks_ok": None, "worktrees_ignored": None,
                          "docs_found": False, "docs": [], "notes": [],
                          "problems": [{"item": "root", "detail": "目录不存在",
                                        "fix": f"这个项目登记的根已经没了：unregister_project 撤销再重新登记，"
                                               f"或把目录恢复回 {root}"}]}
            else:
                report = inspect(root)
                report["project_id"] = project_id
            reports.append(report)

    problems = sum(len(report["problems"]) for report in reports)
    if args.json:
        print(json.dumps({"ok": problems == 0, "result": {
            "checked": len(reports), "problem_count": problems, "projects": reports,
        }}, ensure_ascii=False, indent=2))
    else:
        for report in reports:
            for line in render(report):
                print(line)
        if not reports:
            print("没有已登记项目：没有东西可查，不是失败。", file=sys.stderr)
        print(f"共查 {len(reports)} 个项目，缺 {problems} 项；本脚本只报不改，"
              "补法见每一条缺项后面那句。", file=sys.stderr)
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
