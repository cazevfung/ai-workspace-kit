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
    python install.py --allow-user-config     # 允许写用户级配置（沙箱安装、或重指已有 center.json 时才需要）

装了什么：
  1. 工作区本体（task_center、tools、docs、tests、PRD 模板、tasks 起步文件）
     以及能拿工作区当参数直接跑的校验脚本（verify/verify_receipt.py）
  2. 项目内技能 → <工作区>/.agents/skills
  3. 全部 25 个技能 → <agents-home>/skills（已存在的默认跳过，不覆盖）
  4. <agents-home>/center.json（指向本工作区唯一的中心库）
     与 <工作区>/.agents/center.json（记住这次装的是哪个 agents-home，闸门按它找库）
  5. <agents-home>/intake/（交接协议，供别的项目的会话读取）
  6. 中心库初始化 + 登记本工作区项目
  7. git 仓库 + 提交闸门（先做首次提交，再启用钩子，避免自己拦自己）
  8. 浮窗面板 → <工作区>/panel（正文里的占位符换成真实路径；要 Node 18+ 与 npm install electron 才能跑）

第 3–5 步写的是用户级配置，所以它们都受一道检查：agents-home 不是本机真实的
用户目录时（沙箱 / 临时 / 别人的目录），默认一个字节都不写；agents-home 就是真实
用户目录、而那里的 center.json 已经指向别的库时，默认拒绝并打印原内容。两种情况都
要显式 --allow-user-config 才往下写（判据只有一处：kit/tools/install_guard.py）。

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

# 用户级配置写不写，判据在 kit/tools/install_guard.py 一个文件里——不在这里再写一遍，
# 否则「什么情况算沙箱」「什么叫指着别的库」迟早出现第二个答案。
sys.path.insert(0, str(KIT / "tools"))
from install_guard import decide_user_config  # noqa: E402
# `.mjs` / `.cjs` / `.html` 是浮窗面板的正文后缀：它们里面有工作区占位符，
# 不在这张表里就会被原样复制，装出来的 main.mjs 读不到导出物、样张里全是占位符。
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".jsonl", ".sh", ".ps1", ".toml", ".cfg", ".ini",
                 ".yml", ".yaml", ".mjs", ".cjs", ".html"}

# 装进工作区 verify/ 的校验脚本：只装「拿一个工作区路径当参数就能跑」的那一个。
# `verify_install.py` / `verify_onboard.py` 要调仓库根的 install.py / uninstall.py，
# 那是套件源码树才有的东西；把它们装到别人的工作区里，会得到一份当场就找不到文件的
# 脚本——装了个跑不起来的壳，比没装更难查。所以只复制 RECEIPT 这一个。
VERIFY = HERE / "verify"
WORKSPACE_VERIFIERS = ("verify_receipt.py",)
# 浮窗面板：整棵子树原样装到 <工作区>/panel，正文里的占位符按工作区替换。
PANEL = HERE / "panel"

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
    """把占位换成真实路径。统一用正斜杠：`.json` 里反斜杠要转义，Markdown 里也不好读。"""
    return text.replace("{{WORKSPACE}}", workspace.replace("\\", "/"))


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


def copy_skill_tree(src: Path, dst: Path, workspace: str) -> None:
    """复制技能目录，同时把正文里的 `{{WORKSPACE}}` 换成真实安装路径。

    技能里写占位而不是绝对路径，否则换台机器就指错地方；复制时直接 copytree
    会把占位原样留在用户机器上，调用方就不知道真实的工作区在哪。
    """
    for path in sorted(src.rglob("*")):
        target = dst / path.relative_to(src)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() in TEXT_SUFFIXES:
            try:
                target.write_bytes(template_text(path.read_text(encoding="utf-8"), workspace).encode("utf-8"))
                continue
            except UnicodeDecodeError:
                pass
        shutil.copy2(path, target)


def install_skills(target_root: Path, names: list[str], subdir: str, workspace: str, dry: bool,
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
            copy_skill_tree(src, dst, str(workspace))
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


def copy_workspace_verifiers(workspace: Path, workspace_str: str, dry: bool) -> list[str]:
    """把能独立跑的校验脚本装到 <工作区>/verify/。

    `tools/test_verify_receipt.py` 原本按源码布局取 `ROOT.parent/verify/`（套件仓库里
    它在仓库根），装出来的工作区没有那一层，8 条用例就全红了。装这一步补上文件，
    用例那一侧同时认两种布局（见该文件里的 `find_script`）。
    """
    installed = []
    for name in WORKSPACE_VERIFIERS:
        src = VERIFY / name
        installed.append(name)
        if dry:
            continue
        target = workspace / "verify" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(template_text(src.read_text(encoding="utf-8"), workspace_str).encode("utf-8"))
    return installed


def copy_panel(workspace: Path, workspace_str: str, dry: bool) -> int:
    """把浮窗面板装到 <工作区>/panel，返回复制的文件数。

    `copy_tree` 已经会跳过 `__pycache__` / `.pyc` 并替换文本里的占位符——面板的
    `.mjs` / `.cjs` / `.html` 靠 `TEXT_SUFFIXES` 那三个后缀进这条路径，否则装出来
    的 `main.mjs` 会拿着一串没替换的占位符去读导出物。

    面板不在 `kit/` 里：它是独立的 Electron 子树（`app/` + `tools/` + 样张），装进
    工作区后仍保持同一层结构，`app/main.mjs` 才能按相对位置找到 `docs/样张/sample.html`。
    """
    if not PANEL.is_dir():
        return 0
    return copy_tree(PANEL, workspace / "panel", workspace_str, dry)



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
    parser.add_argument("--allow-user-config", action="store_true",
                        help="允许写用户级配置：沙箱安装（--agents-home 不是本机真实用户目录）时"
                             "默认跳过 center.json / skills / intake；已有 center.json 指向"
                             "别的库时默认拒绝改写。两种情况都要这个开关")
    parser.add_argument("--no-git", action="store_true", help="不初始化 git、不装提交钩子")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写任何文件")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve()
    agents_home = Path(args.agents_home).expanduser().resolve()
    skills_root = Path(args.skills_dir).expanduser().resolve() if args.skills_dir else agents_home / "skills"
    workspace_str = workspace.as_posix()
    workspace_db = workspace / ".data" / "task-center.sqlite3"
    dry = args.dry_run
    decision = decide_user_config(agents_home, workspace_db,
                                  allow_user_config=args.allow_user_config)

    say(f"AI 工作区套件 v{VERSION}")
    say(f"工作区：{workspace}")
    say(f"用户级：{agents_home}")
    say(f"技能：  {skills_root}")
    if decision.sandbox:
        say(f"沙箱安装：{agents_home} 不是本机真实用户目录（{decision.real_agents_home}）")
        say("   " + ("--allow-user-config 已给：仍会写用户级配置" if decision.allowed
                     else "默认不写用户级配置；要写请加 --allow-user-config"))
    if dry:
        say("（预演模式：不会写入任何文件）")

    if not python_ok():
        return 2
    if not KIT.is_dir():
        fail(f"找不到套件内容：{KIT}；请在套件仓库根目录运行")
        return 2
    missing_verifiers = [name for name in WORKSPACE_VERIFIERS if not (VERIFY / name).is_file()]
    if missing_verifiers:
        fail(f"找不到校验脚本：{', '.join(str(VERIFY / n) for n in missing_verifiers)}；"
             "请在套件仓库根目录运行（装出来的工作区里 verify/ 只放跑得起来的那几个）")
        return 2
    if workspace.exists() and any(workspace.iterdir()) and not args.force:
        fail(f"目标目录非空：{workspace}；换一个目录，或加 --force 覆盖")
        return 2
    # 拒绝要发生在复制第一个文件之前：半装出来的工作区比没装更难查。
    if decision.refusal:
        step("0/7 用户级配置检查")
        if dry:
            say("   预演：真装会在这里停下（退出码 2），一个文件都不写")
        else:
            fail(f"拒绝改写用户级配置：{decision.center_path}")
        say(f"   原内容：")
        for line in (decision.existing_text.splitlines() or ["（读不出来）"]):
            say(f"     | {line}")
        say(f"   {decision.refusal}")
        if not dry:
            return 2

    step("1/7 复制工作区本体")
    copied = copy_tree(KIT, workspace, workspace_str, dry)
    say(f"   已复制 {copied} 个文件")
    verifiers = copy_workspace_verifiers(workspace, workspace_str, dry)
    say(f"   已复制校验脚本 {len(verifiers)} 个：{', '.join(f'verify/{n}' for n in verifiers)}")

    step("2/7 安装项目内技能")
    proj_installed, _ = install_skills(workspace / ".agents" / "skills", WORKSPACE_SKILLS,
                                       "workspace", str(workspace), dry, overwrite=True)
    say(f"   项目内技能 {len(proj_installed)} 个")

    step("3/7 安装用户级技能")
    if args.no_skills:
        say("   跳过（--no-skills）")
        installed, skipped = [], []
    elif not decision.allowed and args.skills_dir is None:
        # 沙箱默认不写用户级技能；显式给了 --skills-dir 就算显式指定目标，照写。
        say(f"   跳过：沙箱安装默认不写用户级技能（{skills_root}）")
        say("   要写请加 --allow-user-config（或用 --skills-dir 显式指定一个目标目录）")
        installed, skipped = [], []
    else:
        # 工作区技能与通用技能来源不同，分别处理
        ws_ins, ws_skip = install_skills(skills_root, WORKSPACE_SKILLS, "workspace", str(workspace),
                                         dry, args.overwrite_skills)
        gen_ins, gen_skip = install_skills(skills_root, GENERIC_SKILLS, "generic", str(workspace),
                                           dry, args.overwrite_skills)
        installed, skipped = ws_ins + gen_ins, ws_skip + gen_skip
        say(f"   新装 {len(installed)} 个：{', '.join(installed) if installed else '（无）'}")
        if skipped:
            say(f"   跳过已存在的 {len(skipped)} 个：{', '.join(skipped)}")

    step("4/7 写用户级指针与交接协议")
    center_path = agents_home / "center.json"
    pointer = workspace / ".agents" / "center.json"
    intake = agents_home / "intake"
    manifest = agents_home / ".ai-workspace-kit.json"
    if not decision.allowed:
        say(f"   跳过：沙箱安装默认不写用户级 center.json / intake/ / 安装清单（{agents_home}）")
        say("   要写请加 --allow-user-config")
        # 工作区指针也不写：它记的是 agents_home，而那里没有 center.json 时，闸门按
        # 「指针指丢」报 DB_NOT_FOUND 而不是回落（center_config 的判据），会把一个本来
        # 能用自带库的工作区弄成闭门。不写指针，闸门就走「本工作区 .data」那一步。
        say(f"   工作区指针也不写（{pointer}）：没有 agents_home 的 center.json 时，"
            f"闸门会读到工作区自带的 {workspace_db}")
    elif dry:
        say(f"   预演：写 {center_path}（db → {workspace_db}）")
        say(f"   预演：写工作区指针 {pointer} → {agents_home}")
        say(f"   预演：复制交接协议到 {intake}/（docs 下全部文件，已有则改名备份）")
        say(f"   预演：写安装清单 {manifest}")
    else:
        agents_home.mkdir(parents=True, exist_ok=True)
        center_path.write_text(
            json.dumps({"db": str(workspace_db)}, ensure_ascii=False),
            encoding="utf-8")
        # 工作区自己也要记住「这次安装用的是哪个用户级目录」（AWK-105）。
        # 只记 agents_home，不记 db：库的位置由 <agents_home>/center.json 说了算，
        # 只留一处真相；换了 agents-home 重装一次，这里跟着换。
        # 没有这个文件，闸门只能去读 ~/.agents，--agents-home 装出来的工作区就会
        # 读到另一套库（实测报 SCHEMA_MISMATCH）。
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(
            json.dumps({"agents_home": str(agents_home)}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        say(f"   工作区指针：{pointer} → {agents_home}")
        if intake.exists():
            backup = intake.with_name(f"intake.bak-{os.getpid()}")
            shutil.move(str(intake), str(backup))
            warn(f"已有 {intake}，已改名备份为 {backup.name}")
        copy_tree(KIT / "docs", intake, str(workspace), dry)
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

    step("附：把浮窗面板装到 <工作区>/panel")
    if dry:
        say("   预演：复制 panel/（占位符换成真实路径）")
    else:
        panel_files = copy_panel(workspace, workspace_str, dry)
        if panel_files:
            say(f"   已复制 {panel_files} 个文件：panel/（浮窗要 Node 18+ 与 npm install electron）")
        else:
            warn("没有找到 panel/；跳过浮窗面板（其余部分照常可用）")

    if args.link_agents and not args.no_skills:
        step("附：为其他 agent 建技能链接")
        made = link_agent_dirs(agents_home.parent, skills_root, dry)
        say(f"   新建 {len(made)} 条链接")

    print(f"""
{'预演完成' if dry else '安装完成'}。下一步：

  cd {workspace}
  python onboard.py --json                                           # 在宿主里开工的调用计划
  python tools/work_gate.py --list                                   # 开工第一条命令
  python tools/run_tests.py                                          # 验证环境（应全绿）
  python tools/doc_gate.py --all                                     # 计划格式自检
  python verify/verify_receipt.py --workspace .                      # 开工回执的形状（verify/ 已装进工作区）

在工作区会话里，先读 AGENTS.md；要开工单时按 .agents/skills/ticket-dispatch 的顺序走。
Cindy 里把这个目录加为项目，技能会自动从 {skills_root} 读到。
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
