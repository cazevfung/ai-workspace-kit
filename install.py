#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 工作区套件 · 一键安装。

把一个可用的工作区装到指定目录，并把整套技能装到当前用户的 agent 技能目录。
只依赖 Python 标准库；Git 可选（有则配置提交闸门，没有也能用）。

    python install.py                         # 装到 ~/ai-workspace
    python install.py --workspace D:/ai-ws    # 指定目录
    python install.py --dry-run               # 只打印计划，不动文件
    python install.py --force                 # 目标目录已存在时覆盖
    python install.py --no-skills             # 只装工作区，不动技能目录
    python install.py --overwrite-skills      # 用套件版本覆盖同名技能（默认跳过）
    python install.py --link-agents           # 顺带为 ~/.claude 与 ~/.codex 建技能链接
    python install.py --agents-home <目录>     # 改用户级目录（默认 ~/.agents，供测试用）

装了什么：
  1. 工作区本体（task_center、tools、docs、tests、PRD 模板、tasks 起步文件）
  2. 项目内技能 → <工作区>/.agents/skills
  3. 全部 25 个技能 → <agents-home>/skills（已存在的默认跳过，不覆盖）
  4. <agents-home>/center.json（指向本工作区唯一的中心库）
  5. <agents-home>/intake/（交接协议，供别的项目的会话读取）
  6. 中心库初始化 + 登记本工作区项目
  7. git 仓库 + 提交闸门（先做首次提交，再启用钩子，避免自己拦自己）

退出码：0 成功；1 有步骤失败；2 用法或环境不满足。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIT = HERE / "kit"
SKILLS = HERE / "skills"
VERSION = (HERE / "VERSION").read_text(encoding="utf-8").strip() if (HERE / "VERSION").is_file() else "0.0.0"
PROJECT_ID = "ai-workspace"
PROJECT_NAME = "AI 工作区"
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".jsonl", ".sh", ".ps1", ".toml", ".cfg", ".ini", ".yml", ".yaml"}

WORKSPACE_SKILLS = sorted(p.name for p in (SKILLS / "workspace").iterdir() if (p / "SKILL.md").is_file())
GENERIC_SKILLS = sorted(p.name for p in (SKILLS / "generic").iterdir() if (p / "SKILL.md").is_file())


def say(message: str = "") -> None:
    print(message, flush=True)


def step(message: str) -> None:
    say(f"\n== {message}")


def warn(message: str) -> None:
    say(f"   ! {message}")


def fail(message: str) -> None:
    say(f"   × {message}")


def template_text(text: str, workspace: str) -> str:
    return text.replace("{{WORKSPACE}}", workspace)


def copy_tree(src: Path, dst: Path, workspace: str, dry: bool) -> int:
    count = 0
    for path in sorted(src.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(src)
        if "__pycache__" in rel.parts or path.suffix == ".pyc":
            continue
        target = dst / rel
        count += 1
        if dry:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() in TEXT_SUFFIXES:
            try:
                target.write_bytes(template_text(path.read_text(encoding="utf-8"), workspace).encode("utf-8"))
                continue
            except UnicodeDecodeError:
                pass
        shutil.copy2(path, target)
    return count


def python_ok() -> bool:
    if sys.version_info < (3, 10):
        fail(f"需要 Python 3.10 或更高，当前是 {sys.version.split()[0]}")
        return False
    return True


def git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def git_identity_ok(workspace: Path) -> bool:
    try:
        email = subprocess.run(["git", "-C", str(workspace), "config", "user.email"],
                               capture_output=True, text=True, check=True).stdout.strip()
        name = subprocess.run(["git", "-C", str(workspace), "config", "user.name"],
                              capture_output=True, text=True, check=True).stdout.strip()
        return bool(email and name)
    except (OSError, subprocess.CalledProcessError):
        return False


def install_skills(target_root: Path, names: list[str], subdir: str, dry: bool,
                   overwrite: bool) -> tuple[list[str], list[str]]:
    installed, skipped = [], []
    if not dry:
        target_root.mkdir(parents=True, exist_ok=True)
    for name in names:
        src = SKILLS / subdir / name
        dst = target_root / name
        if dst.exists() and not overwrite:
            skipped.append(name)
            continue
        if not dry:
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        installed.append(name)
    return installed, skipped


def link_agent_dirs(home: Path, skills_root: Path, dry: bool) -> list[str]:
    """把 <home>/.claude/skills 与 .codex/skills 里缺失的条目链到 skills_root。

    只建链接，不复制；目标已存在就不动。Windows 用目录联接（不需管理员），其他平台用符号链接。
    """
    made = []
    for agent_dir in (home / ".claude" / "skills", home / ".codex" / "skills"):
        if not agent_dir.parent.exists():
            continue
        if not dry:
            agent_dir.mkdir(parents=True, exist_ok=True)
        for src in sorted(skills_root.iterdir()):
            if not src.is_dir():
                continue
            dst = agent_dir / src.name
            if dst.exists():
                continue
            if not dry:
                try:
                    if os.name == "nt":
                        subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                                       capture_output=True, check=True)
                    else:
                        dst.symlink_to(src, target_is_directory=True)
                except (OSError, subprocess.CalledProcessError):
                    continue
            made.append(str(dst))
    return made


def init_center(workspace: Path, dry: bool) -> bool:
    db = workspace / ".data" / "task-center.sqlite3"
    if dry:
        say(f"   预演：初始化 {db} 并登记 {PROJECT_ID}")
        return True
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from task_center.core import Center;"
        "c=Center(sys.argv[2]);"
        "print(c.execute('init', {}, None));"
        "print(c.execute('register_project', {'project_id': sys.argv[3], 'name': sys.argv[4], 'root': sys.argv[5]}, 'install-register'))"
    )
    # -X utf8 + errors="replace"：子进程的输出走管道时按 locale（中文 Windows 上是 cp936）编码，
    # 父进程按 utf-8 解码会先在读取线程里抛 UnicodeDecodeError，安装步骤本身却照样成功。
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", code, str(workspace), str(db),
                             PROJECT_ID, PROJECT_NAME, str(workspace)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        fail("中心库初始化失败：")
        say(result.stdout + result.stderr)
        return False
    say(f"   中心库：{db}")
    say(f"   已登记项目：{PROJECT_ID} → {workspace}")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="安装 AI 工作区套件")
    parser.add_argument("--workspace", default=str(Path.home() / "ai-workspace"),
                        help="工作区安装目录（默认 ~/ai-workspace）")
    parser.add_argument("--agents-home", default=str(Path.home() / ".agents"),
                        help="用户级技能与配置目录（默认 ~/.agents）")
    parser.add_argument("--skills-dir", default=None,
                        help="技能安装目录（默认 <agents-home>/skills）")
    parser.add_argument("--force", action="store_true", help="目标目录已存在时覆盖")
    parser.add_argument("--no-skills", action="store_true", help="不安装技能")
    parser.add_argument("--overwrite-skills", action="store_true", help="覆盖同名技能（默认跳过）")
    parser.add_argument("--link-agents", action="store_true", help="顺带为 ~/.claude 与 ~/.codex 建技能链接")
    parser.add_argument("--no-git", action="store_true", help="不初始化 git、不装提交钩子")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写任何文件")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve()
    agents_home = Path(args.agents_home).expanduser().resolve()
    skills_root = Path(args.skills_dir).expanduser().resolve() if args.skills_dir else agents_home / "skills"
    workspace_str = workspace.as_posix()
    dry = args.dry_run

    say(f"AI 工作区套件 v{VERSION}")
    say(f"工作区：{workspace}")
    say(f"用户级：{agents_home}")
    say(f"技能：  {skills_root}")
    if dry:
        say("（预演模式：不会写入任何文件）")

    if not python_ok():
        return 2
    if not KIT.is_dir():
        fail(f"找不到套件内容：{KIT}；请在套件仓库根目录运行")
        return 2
    if workspace.exists() and any(workspace.iterdir()) and not args.force:
        fail(f"目标目录非空：{workspace}；换一个目录，或加 --force 覆盖")
        return 2

    step("1/7 复制工作区本体")
    copied = copy_tree(KIT, workspace, workspace_str, dry)
    say(f"   已复制 {copied} 个文件")

    step("2/7 安装项目内技能")
    proj_installed, _ = install_skills(workspace / ".agents" / "skills", WORKSPACE_SKILLS,
                                       "workspace", dry, overwrite=True)
    say(f"   项目内技能 {len(proj_installed)} 个")

    step("3/7 安装用户级技能")
    if args.no_skills:
        say("   跳过（--no-skills）")
        installed, skipped = [], []
    else:
        # 工作区技能与通用技能来源不同，分别处理
        ws_ins, ws_skip = install_skills(skills_root, WORKSPACE_SKILLS, "workspace", dry, args.overwrite_skills)
        gen_ins, gen_skip = install_skills(skills_root, GENERIC_SKILLS, "generic", dry, args.overwrite_skills)
        installed, skipped = ws_ins + gen_ins, ws_skip + gen_skip
        say(f"   新装 {len(installed)} 个：{', '.join(installed) if installed else '（无）'}")
        if skipped:
            say(f"   跳过已存在的 {len(skipped)} 个：{', '.join(skipped)}")

    step("4/7 写用户级指针与交接协议")
    if dry:
        say("   预演：写 center.json、intake/、安装清单")
    else:
        agents_home.mkdir(parents=True, exist_ok=True)
        (agents_home / "center.json").write_text(
            json.dumps({"db": str(workspace / ".data" / "task-center.sqlite3")}, ensure_ascii=False),
            encoding="utf-8")
        intake = agents_home / "intake"
        if intake.exists():
            backup = intake.with_name(f"intake.bak-{os.getpid()}")
            shutil.move(str(intake), str(backup))
            warn(f"已有 {intake}，已改名备份为 {backup.name}")
        shutil.copytree(KIT / "docs", intake)
        manifest = agents_home / ".ai-workspace-kit.json"
        manifest.write_text(json.dumps({
            "version": VERSION,
            "workspace": str(workspace),
            "skills_dir": str(skills_root),
            "installed_skills": installed,
            "skipped_skills": skipped,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    step("5/7 初始化中心库并登记项目")
    if not init_center(workspace, dry):
        return 1

    step("6/7 建立 git 仓库并做首次提交")
    if args.no_git:
        say("   跳过（--no-git）")
    elif not git_available():
        warn("没有找到 git；跳过仓库初始化与提交闸门（工作区仍可使用）")
    elif dry:
        say("   预演：git init → add → commit（首次提交后再启用钩子）")
    else:
        if not (workspace / ".git").exists():
            subprocess.run(["git", "-C", str(workspace), "init"], capture_output=True, check=False)
        if git_identity_ok(workspace):
            subprocess.run(["git", "-C", str(workspace), "add", "-A"], capture_output=True, check=False)
            commit = subprocess.run(["git", "-C", str(workspace), "commit", "-m", "初始化：AI 工作区套件"],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace")
            if commit.returncode == 0:
                say("   已做首次提交")
            else:
                warn("首次提交失败（可能没有改动或 git 配置不全），可稍后手工提交")
        else:
            warn("git 缺 user.name / user.email，跳过首次提交；配置后再提交即可")

    step("7/7 启用提交闸门")
    if dry:
        say("   预演：git config core.hooksPath .githooks")
    elif not args.no_git and git_available() and (workspace / ".git").exists():
        subprocess.run(["git", "-C", str(workspace), "config", "core.hooksPath", ".githooks"],
                       capture_output=True, check=False)
        say("   已启用：没工单的改动无法提交（这是默认安全路径，可显式关闭）")

    if args.link_agents and not args.no_skills:
        step("附：为其他 agent 建技能链接")
        made = link_agent_dirs(agents_home.parent, skills_root, dry)
        say(f"   新建 {len(made)} 条链接")

    print(f"""
{'预演完成' if dry else '安装完成'}。下一步：

  cd {workspace}
  python tools/work_gate.py --list                                   # 开工第一条命令
  python tools/run_tests.py                                          # 验证环境（应全绿）
  python tools/doc_gate.py --all                                     # 计划格式自检

在工作区会话里，先读 AGENTS.md；要开工单时按 .agents/skills/ticket-dispatch 的顺序走。
Cindy 里把这个目录加为项目，技能会自动从 {skills_root} 读到。
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
