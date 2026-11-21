#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一个业务仓库接进 AI 工作区：写项目配置，并把提交闸门指过来。

为什么要有这个脚本：工作区装好之后，业务项目仍然是「另一个目录」。`auto_merge`
要知道这个项目的测试命令与目标分支，`git commit` 要知道哪套钩子该拦它。靠人在
两个仓库里各手敲一遍 `git config`，漏一次就退回「没人知道这次改动该跑什么测试」。

    python tools/add_project.py D:/my-project
    python tools/add_project.py D:/my-project --test-cmd "python -m pytest -q"
    python tools/add_project.py D:/my-project --target-branch main --force

写什么：
  1. `<仓库>/.agents/ticket-config.json`：`version` / `project_id` / `target_branch` /
     `test_cmd` / `test_timeout`（不写套件的绝对路径：配置会跟仓库走，换台机器就指错）；
  2. `git -C <仓库> config core.hooksPath <套件>/.githooks`。

`test_cmd` 存数组，`auto_merge` 直接 `subprocess.run`，不经过 shell；命令行参数用
`--test-cmd` 传入时按 `shlex.split` 拆开。没传时会按仓库形貌推断：有
`tools/run_tests.py` 用它，否则用 `python -m unittest discover -s tests`。

退出码：0 写好；1 环境不满足（目录不存在、不是 git 仓库、钩子不存在）；2 用法错误。
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent.parent
CONFIG_REL = Path(".agents") / "ticket-config.json"
DEFAULT_TEST_CMD = ["python", "-m", "unittest", "discover", "-s", "tests"]


class AddProjectError(RuntimeError):
    """带一句人能读的补救。main 打到 stderr 并非零退出。"""


def git(repo: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AddProjectError(f"跑不了 git：{exc}。确认 git 已安装并在 PATH 里。") from exc


def git_out(repo: Path, *args: str) -> str:
    result = git(repo, *args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise AddProjectError(
            f"`git {' '.join(args)}` 退出 {result.returncode}（{detail[0] if detail else '无输出'}）。"
        )
    return result.stdout.strip()


def detect_test_cmd(repo: Path) -> list[str]:
    if (repo / "tools" / "run_tests.py").is_file():
        return ["python", "tools/run_tests.py"]
    if (repo / "tests").is_dir():
        return list(DEFAULT_TEST_CMD)
    return list(DEFAULT_TEST_CMD)


def build_config(repo: Path, *, target_branch: str, test_cmd: list[str], test_timeout: int,
                 project_id: str | None = None) -> dict:
    return {
        "version": 1,
        "project_id": project_id or repo.name,
        "target_branch": target_branch,
        "test_cmd": list(test_cmd),
        "test_timeout": int(test_timeout),
    }


def require_git_repo(repo: Path) -> None:
    result = git(repo, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        raise AddProjectError(
            f"{repo} 不是 git 仓库（或不在仓库里）。先在项目根跑 `git init` 并提交一次，再运行本脚本。"
        )
    top = Path(result.stdout.strip()).resolve()
    if top != repo.resolve():
        raise AddProjectError(
            f"请传仓库根：{repo} 在 {top} 里面。钩子与配置都按仓库根写，子目录会写成两份。"
        )


def current_branch(repo: Path) -> str:
    result = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    branch = result.stdout.strip()
    if result.returncode != 0 or not branch:
        raise AddProjectError(
            "当前是游离 HEAD，判不出目标分支。用 `--target-branch <分支名>` 明确指定。"
        )
    return branch


def add_project(repo: Path, *, target_branch: str | None = None, test_cmd: str | None = None,
                test_timeout: int = 1800, force: bool = False, hooks: Path | None = None,
                dry_run: bool = False) -> dict:
    repo = Path(repo).expanduser().resolve()
    if not repo.is_dir():
        raise AddProjectError(f"仓库目录不存在：{repo}。传一个已经存在的绝对路径。")
    require_git_repo(repo)

    hooks = (Path(hooks).expanduser().resolve() if hooks else HERE / ".githooks")
    hook = hooks / "pre-commit"
    if not hook.is_file():
        raise AddProjectError(
            f"套件钩子不存在：{hook}。确认工作区安装完整（install.py 会把 .githooks 一起装上）。"
        )

    config_path = repo / CONFIG_REL
    if config_path.exists() and not force:
        raise AddProjectError(
            f"{config_path} 已存在。要改测试命令或目标分支，直接编辑它；"
            "确实要按本脚本重写，加 `--force`。"
        )

    branch = target_branch or current_branch(repo)
    if test_cmd:
        command = shlex.split(test_cmd, posix=False)
        command = [part.strip('"') for part in command]
    else:
        command = detect_test_cmd(repo)
    config = build_config(repo, target_branch=branch, test_cmd=command, test_timeout=test_timeout)

    if not dry_run:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result = git(repo, "config", "core.hooksPath", hooks.as_posix())
        if result.returncode != 0:
            raise AddProjectError(
                f"写 core.hooksPath 失败：{(result.stderr or result.stdout).strip()}。"
                "手工执行 `git -C <仓库> config core.hooksPath <套件>/.githooks` 后重试。"
            )
    return {
        "repo": repo.as_posix(),
        "config_path": (repo / CONFIG_REL).as_posix(),
        "config": config,
        "hooks_path": hooks.as_posix(),
        "dry_run": dry_run,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="把业务仓库接进 AI 工作区")
    parser.add_argument("repo", help="业务仓库绝对路径")
    parser.add_argument("--target-branch", default=None, help="合并目标分支（默认当前分支）")
    parser.add_argument("--test-cmd", default=None, help="测试命令；缺省按仓库形貌推断")
    parser.add_argument("--test-timeout", type=int, default=1800, help="测试超时秒数（默认 1800）")
    parser.add_argument("--hooks", default=None, help="钩子目录（默认套件 .githooks）")
    parser.add_argument("--force", action="store_true", help="配置已存在时重写")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要写什么")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args(argv)
    try:
        result = add_project(
            Path(args.repo), target_branch=args.target_branch, test_cmd=args.test_cmd,
            test_timeout=args.test_timeout, force=args.force,
            hooks=Path(args.hooks) if args.hooks else None, dry_run=args.dry_run,
        )
    except AddProjectError as exc:
        print(f"接项目失败：{exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
        return 0
    print(f"{'预演' if args.dry_run else '已接项目'}：{result['repo']}")
    print(f"  配置：{result['config_path']}")
    print(f"  目标分支：{result['config']['target_branch']}")
    print(f"  测试命令：{shlex.join(result['config']['test_cmd'])}")
    print(f"  提交钩子：{result['hooks_path']}")
    if args.dry_run:
        print("（--dry-run：没有写任何文件，也没有改 git 配置）")
    else:
        print("下一步：在这个仓库里开工单并提交；没有 running 工单的提交会被钩子拦下。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
