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
        check("项目内技能 5 个", len([p for p in (ws / ".agents" / "skills").iterdir()]) == 5)
        check("用户级技能 25 个", len([p for p in (agents / "skills").iterdir()]) == 25)
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
        check("交接协议已复制", (agents / "intake" / "intake.md").is_file())
        check("安装清单已写", (agents / ".ai-workspace-kit.json").is_file())

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
