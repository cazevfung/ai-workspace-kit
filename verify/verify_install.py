#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安装自检：在临时目录真装一遍，确认装出来的东西能跑。

不碰真实的 ~/.agents：用 --agents-home 把用户级目录重定向到临时目录。

    python verify/verify_install.py            # 完整自检（约 1–2 分钟）
    python verify/verify_install.py --keep     # 保留临时目录，便于排查

退出码：0 全部通过；1 有检查失败。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent


def run(cmd, cwd, timeout=600):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="安装自检")
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    args = parser.parse_args(argv)

    base = Path(tempfile.mkdtemp(prefix="aiws-verify-"))
    ws = base / "ws"
    agents = base / "agents"
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, ok, detail))
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"), flush=True)

    try:
        print(f"临时目录：{base}\n")
        result = run([sys.executable, str(ROOT / "install.py"),
                      "--workspace", str(ws), "--agents-home", str(agents), "--no-git"], ROOT, timeout=300)
        check("install.py 退出 0", result.returncode == 0,
              (result.stdout + result.stderr)[-1500:])

        check("工作区核心存在", (ws / "task_center" / "core.py").is_file())
        check("项目内技能 7 个", len([p for p in (ws / ".agents" / "skills").iterdir()]) == 7)
        check("用户级技能 27 个", len([p for p in (agents / "skills").iterdir()]) == 27)
        center_path = agents / "center.json"
        center_ok = False
        if center_path.is_file():
            try:
                import json
                db_in_config = json.loads(center_path.read_text(encoding="utf-8")).get("db", "")
                center_ok = Path(db_in_config).resolve() == (ws / ".data" / "task-center.sqlite3").resolve()
            except Exception:  # noqa: BLE001
                center_ok = False
        check("center.json 指向本工作区", center_ok, str(center_path))
        # AWK-105：工作区自己也要记住这次装的是哪个 agents-home，否则闸门只会
        # 去读 ~/.agents，--agents-home 装出来的工作区就闭门读到另一套库。
        pointer_path = ws / ".agents" / "center.json"
        pointer_ok = False
        if pointer_path.is_file():
            try:
                import json
                pointer_ok = Path(json.loads(pointer_path.read_text(encoding="utf-8"))
                                  .get("agents_home", "")).resolve() == agents.resolve()
            except Exception:  # noqa: BLE001
                pointer_ok = False
        check("工作区指针记下了 --agents-home", pointer_ok, str(pointer_path))

        # 真实现场：装完之后闸门读到的是这一份库（不是用户真实 ~/.agents 那一份）。
        created_db = (ws / ".data" / "task-center.sqlite3").resolve()
        result = run([sys.executable, "tools/work_gate.py", "--list"], ws, timeout=120)
        gate_output = result.stdout + result.stderr
        gate_ok = (result.returncode == 0 and "SCHEMA_MISMATCH" not in gate_output
                   and str(created_db) in gate_output)
        check("work_gate --list 读的是本工作区的库", gate_ok, gate_output[-800:])

        # 把 <agents-home>/center.json 临时改名：必须报 DB_NOT_FOUND 且非零退出，
        # 不许静默回落到 ~/.agents，也不许新建一个空库（一个文件都不许多）。
        before = sorted(p.name for p in (ws / ".data").iterdir())
        stash = center_path.with_name("center.json.moved")
        center_path.rename(stash)
        try:
            result = run([sys.executable, "tools/work_gate.py", "--list"], ws, timeout=120)
            output = result.stdout + result.stderr
            after = sorted(p.name for p in (ws / ".data").iterdir())
            check("agents-home 配置缺失时 DB_NOT_FOUND 非零退出",
                  result.returncode != 0 and "DB_NOT_FOUND" in output and after == before,
                  output[-800:])
        finally:
            stash.rename(center_path)
        check("交接协议已复制", (agents / "intake" / "intake.md").is_file())
        check("安装清单已写", (agents / ".ai-workspace-kit.json").is_file())

        # 浮窗面板：装到 <工作区>/panel，且占位符真的换成了这台机器上的路径。
        # 两条分开报：「有没有装」与「装出来的是不是指对地方」是两件事——.mjs / .cjs / .html
        # 曾经不在 install.py 的替换后缀里，那一步会让文件装过去却留着占位符。
        panel_main = ws / "panel" / "app" / "main.mjs"
        check("panel 已装到工作区", panel_main.is_file())
        panel_files = [p for p in (ws / "panel").rglob("*") if p.is_file()] if (ws / "panel").is_dir() else []
        panel_placeholders, panel_main_text = [], ""
        for path in panel_files:
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if "{{WORKSPACE}}" in text:
                panel_placeholders.append(path)
            if path == panel_main:
                panel_main_text = text
        check("panel 占位已换成真实路径",
              bool(panel_files) and not panel_placeholders and ws.resolve().as_posix() in panel_main_text,
              f"留着占位符的 {len(panel_placeholders)} 个（如 {', '.join(str(p) for p in panel_placeholders[:3])}）；"
              f"main.mjs 里{'有' if ws.resolve().as_posix() in panel_main_text else '没有'}本工作区路径")

        leftovers = []
        for tree in (agents / "skills", agents / "intake"):
            for path in tree.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    if "{{WORKSPACE}}" in path.read_text(encoding="utf-8"):
                        leftovers.append(path)
                except (UnicodeDecodeError, OSError):
                    continue
        check("占位已换成真实路径", not leftovers, ", ".join(str(p) for p in leftovers[:3]))

        db = ws / ".data" / "task-center.sqlite3"
        result = run([sys.executable, "-m", "task_center", "--db", str(db), "list_projects"], ws, timeout=120)
        check("中心库可查询且已登记项目", result.returncode == 0 and "ai-workspace" in result.stdout,
              (result.stdout + result.stderr)[-800:])

        result = run([sys.executable, "tools/doc_gate.py", "--all"], ws, timeout=120)
        check("doc_gate --all 退出 0", result.returncode == 0,
              (result.stdout + result.stderr)[-1500:])

        run(["git", "init"], ws, timeout=60)  # 部分闸门用例需要仓库存在
        result = run([sys.executable, "tools/run_tests.py"], ws, timeout=900)
        ok = result.returncode == 0 and "结果：全部通过" in result.stdout
        check("run_tests 全绿", ok, (result.stdout + result.stderr)[-2000:])

        # 卸载：只动临时目录
        result = run([sys.executable, str(ROOT / "uninstall.py"), "--workspace", str(ws),
                      "--agents-home", str(agents), "--yes"], ROOT, timeout=120)
        check("uninstall.py 退出 0", result.returncode == 0,
              (result.stdout + result.stderr)[-800:])
        check("卸载后技能已移除", not (agents / "skills" / "brainstorming").exists())
        check("卸载后进度库保留", (ws / ".data" / "task-center.sqlite3").exists())

    except Exception as exc:  # noqa: BLE001 - 自检脚本需要把异常也报成失败
        check("自检未抛异常", False, repr(exc))
    finally:
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)
        else:
            print(f"\n保留临时目录：{base}")

    failed = [c for c in checks if not c[1]]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} 项通过")
    if failed:
        print("失败项：")
        for name, _, detail in failed:
            print(f" - {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
