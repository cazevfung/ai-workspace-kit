#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""九步自检：临时 HOME 里真装一遍，再从「接项目」走到「收尾」。

为什么要有这个脚本：`verify_install.py` 只证明文件装对了；「装完能不能在宿主里
开工」还要证明四件事——工作区能给出启动计划、业务仓库能接进来、工单能走完闭环、
收尾闸门认这份成果。这些步骤以前各有一半证据，没有一条命令把它们串起来。

    python verify/verify_onboard.py            # 完整自检（约 1–3 分钟）
    python verify/verify_onboard.py --keep     # 保留临时目录，便于排查

九步逐行打印：install / add-project / create_ticket / claim / bind / submit /
review / merge / close。任一步失败即非零退出，并保留失败现场（加 --keep）。

不碰真实 HOME：`USERPROFILE` 与 `HOME` 都指向临时目录，git 全局配置也重定向；
不碰真实 `~/.agents`；不联网。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
STEPS = ("install", "add-project", "create_ticket", "claim", "bind", "submit", "review", "merge", "close")


class VerifyError(RuntimeError):
    pass


def run(cmd, cwd, env, timeout=900):
    return subprocess.run(
        [str(part) for part in cmd], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, env=env,
    )


def git(repo, *args, env, check=True):
    result = run(["git", "-C", str(repo), *args], cwd=repo, env=env, timeout=120)
    if check and result.returncode != 0:
        raise VerifyError(f"git {' '.join(args)} 退出 {result.returncode}：{(result.stderr or result.stdout).strip()}")
    return result


def require_ok(reply, what):
    if not reply.get("ok"):
        raise VerifyError(f"{what} 失败：{json.dumps(reply.get('error', reply), ensure_ascii=False)}")
    return reply["result"]


class Context:
    def __init__(self, base: Path, env: dict, keep: bool):
        self.base = base
        self.env = env
        self.keep = keep
        self.ws = base / "ws"
        # 用户级目录放在临时 HOME 下面：install.py 会写 center.json，
        # 业务仓库的提交钩子靠 ~/.agents/center.json 找到同一个中心库。
        self.agents = base / "home" / ".agents"
        self.repo = base / "business"
        self.center = None
        self.ticket_id = "VERIFY-001"
        self.project_id = "verify-business"
        self.attempt_id = None
        self.submission_id = None
        self.head_sha = None
        self.target_branch = "master"

    def center_handle(self):
        if self.center is None:
            sys.path.insert(0, str(self.ws))
            from task_center.core import Center  # noqa: PLC0415 - 装完才知道代码在哪
            self.center = Center(str(self.ws / ".data" / "task-center.sqlite3"))
        return self.center

    def call(self, operation, payload):
        return require_ok(
            self.center_handle().execute(operation, payload, f"verify-onboard-{uuid.uuid4().hex}"),
            operation,
        )


def step_install(ctx: Context) -> str:
    result = run(
        [sys.executable, str(ROOT / "install.py"), "--workspace", str(ctx.ws),
         "--agents-home", str(ctx.agents), "--no-git"],
        cwd=ROOT, env=ctx.env,
    )
    if result.returncode != 0:
        raise VerifyError(f"install.py 退出 {result.returncode}：{(result.stdout + result.stderr)[-1500:]}")
    for relative in ("task_center/core.py", "tools/run_tests.py", "tools/add_project.py", "onboard.py", ".githooks/pre-commit"):
        if not (ctx.ws / relative).is_file():
            raise VerifyError(f"安装结果缺 {relative}")
    for label, path in (
        ("项目技能", ctx.ws / ".agents" / "skills" / "bootstrap-workspace" / "SKILL.md"),
        ("用户技能", ctx.agents / "skills" / "bootstrap-workspace" / "SKILL.md"),
    ):
        if not path.is_file():
            raise VerifyError(f"{label}缺 {path}")

    # C3：装出来的工作区能打印计划；预演前后项目数不变。
    before = len(require_ok(ctx.center_handle().execute("list_projects", {}), "list_projects")["items"])
    plan = run([sys.executable, "onboard.py", "--json"], cwd=ctx.ws, env=ctx.env)
    if plan.returncode != 0:
        raise VerifyError(f"onboard.py --json 退出 {plan.returncode}：{plan.stderr[-800:]}")
    payload = json.loads(plan.stdout)
    tools = [step["tool"] for step in payload["plans"]["bootstrap"]["steps"]]
    if "create_project" not in tools or "send_to_session" not in tools:
        raise VerifyError(f"启动计划缺 create_project / send_to_session：{tools}")
    for step in payload["plans"]["bootstrap"]["steps"]:
        if not step.get("args"):
            raise VerifyError(f"启动计划步骤 {step['tool']} 没有完整参数")
    dry = run([sys.executable, "onboard.py", "--dry-run"], cwd=ctx.ws, env=ctx.env)
    if dry.returncode != 0:
        raise VerifyError(f"onboard.py --dry-run 退出 {dry.returncode}：{dry.stderr[-800:]}")
    after = len(require_ok(ctx.center_handle().execute("list_projects", {}), "list_projects")["items"])
    if before != after:
        raise VerifyError(f"预演改了项目数：{before} → {after}")
    return f"工作区 {ctx.ws.name}，项目 {before} 个，启动计划 {len(tools)} 步"


def step_add_project(ctx: Context) -> str:
    ctx.repo.mkdir(parents=True, exist_ok=True)
    (ctx.repo / "PRD").mkdir(parents=True, exist_ok=True)
    (ctx.repo / "PRD" / "PRD-001-demo.md").write_text(
        "# PRD-001 演示\n\n版本：v0.1\n\n这是 verify_onboard.py 的临时 PRD。\n", encoding="utf-8")
    (ctx.repo / "README.md").write_text("demo business repository\n", encoding="utf-8")
    git(ctx.repo, "init", "-b", "master", env=ctx.env)
    git(ctx.repo, "add", "-A", env=ctx.env)
    git(ctx.repo, "commit", "-m", "business base", env=ctx.env)
    result = run([sys.executable, "tools/add_project.py", str(ctx.repo), "--test-cmd", "python -c pass"],
                 cwd=ctx.ws, env=ctx.env)
    if result.returncode != 0:
        raise VerifyError(f"add_project.py 退出 {result.returncode}：{(result.stdout + result.stderr)[-1200:]}")
    config_path = ctx.repo / ".agents" / "ticket-config.json"
    if not config_path.is_file():
        raise VerifyError(f"没有写出 {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    for key in ("test_cmd", "target_branch"):
        if not config.get(key):
            raise VerifyError(f"ticket-config.json 缺 {key}")
    ctx.target_branch = config["target_branch"]
    hooks = git(ctx.repo, "config", "core.hooksPath", env=ctx.env).stdout.strip()
    if Path(hooks).resolve() != (ctx.ws / ".githooks").resolve():
        raise VerifyError(f"core.hooksPath 指向 {hooks}，不是套件 .githooks")

    return f"{ctx.repo.name} 已接项目，钩子 → {hooks}"


def step_create_ticket(ctx: Context) -> str:
    # 号由库里发（schema 11 起）：调用方不报 `ticket_id`，以回执里的为真号。
    # 验收条件也得写成闸门认的形状：反引号里的命令，或自称「判断类」并给出反例。
    result = ctx.call("create_ticket", {
        "project_id": ctx.project_id,
        "title": "verify_onboard 自检工单",
        "deliverable": "主题：九步自检里的一张真实工单。不覆盖：不验证真实宿主会话。",
        "prd_path": "PRD/PRD-001-demo.md",
        "criteria": [{"id": "C1",
                      "text": "含 `python verify/verify_onboard.py`，本脚本九步全部跑完并留下记录"}],
        "root": str(ctx.repo),
    })
    if result["state"] != "ready":
        raise VerifyError(f"新工单 state={result['state']}，不是 ready")
    ctx.ticket_id = result["id"]
    return f"{ctx.ticket_id} ready，revision={result['revision']}"


def step_claim(ctx: Context) -> str:
    result = ctx.call("claim_ticket", {
        "project_id": ctx.project_id,
        "ticket_id": ctx.ticket_id,
        "expected_revision": 1,
        "coordinator_session_id": "verify-coordinator-session",
        "coordinator_workdir": str(ctx.ws),
    })
    ctx.attempt_id = result["attempt_id"]
    if result["state"] != "dispatching":
        raise VerifyError(f"claim 后 state={result['state']}，不是 dispatching")
    return f"attempt={ctx.attempt_id[:8]}… dispatching"


def step_bind(ctx: Context) -> str:
    result = ctx.call("bind_session", {
        "project_id": ctx.project_id,
        "ticket_id": ctx.ticket_id,
        "attempt_id": ctx.attempt_id,
        "session_id": "verify-executor-session",
        "project_root": str(ctx.repo),
    })
    if result["state"] != "running":
        raise VerifyError(f"bind 后 state={result['state']}，不是 running")
    return "执行会话已绑定（不等于宿主已核实身份）"


def step_submit(ctx: Context) -> str:
    (ctx.repo / "result.md").write_text("# 自检成果\n\n九步闭环由 verify_onboard.py 跑过。\n", encoding="utf-8")
    git(ctx.repo, "add", "-A", env=ctx.env)
    git(ctx.repo, "commit", "-m", "verify_onboard: result",
        env={**ctx.env, "TASK_CENTER_TICKET": ctx.ticket_id})
    ctx.head_sha = git(ctx.repo, "rev-parse", "HEAD", env=ctx.env).stdout.strip().lower()
    if len(ctx.head_sha) != 40 or any(ch not in "0123456789abcdef" for ch in ctx.head_sha):
        raise VerifyError(f"git rev-parse HEAD 不是 40 位十六进制：{ctx.head_sha!r}")
    result = ctx.call("submit_result", {
        "project_id": ctx.project_id,
        "ticket_id": ctx.ticket_id,
        "attempt_id": ctx.attempt_id,
        "session_id": "verify-executor-session",
        "summary": "verify_onboard.py 九步自检：装、接项目、开单、领取、绑定、提交。",
        "artifacts": ["result.md"],
        "evidence": ["verify/verify_onboard.py 在临时 HOME 里跑完九步，本步产出 result.md"],
        "risks": [],
        "head_sha": ctx.head_sha,
        # schema 10 起两棵树都必填：head 说「交的是哪一棵」，base 说「从哪分出来的」。
        # 这份自检历史只有一个提交，所以 base 与 head 相同——如实记这一次的实际情形。
        "base_sha": ctx.head_sha,
    })
    ctx.submission_id = result["submission_id"]
    if result["state"] != "awaiting_review":
        raise VerifyError(f"submit 后 state={result['state']}，不是 awaiting_review")
    return f"submission={ctx.submission_id[:8]}… sha={ctx.head_sha[:12]}…"


def step_review(ctx: Context) -> str:
    result = ctx.call("review_result", {
        "project_id": ctx.project_id,
        "ticket_id": ctx.ticket_id,
        "attempt_id": ctx.attempt_id,
        "submission_id": ctx.submission_id,
        "reviewer_session_id": "verify-reviewer-session",
        "verdict": "accept",
        "checks": [{"id": "C1", "passed": True, "evidence": "verify_onboard.py 的九步输出与提交 sha 已核对"}],
        "reason": "临时工作区里的九步自检通过。",
    })
    if result["state"] != "done":
        raise VerifyError(f"review 后 state={result['state']}，不是 done")
    return "独立验收会话 accept → done"


def step_merge(ctx: Context) -> str:
    ctx.call("record_notice", {
        "project_id": ctx.project_id,
        "ticket_id": ctx.ticket_id,
        "attempt_id": ctx.attempt_id,
        "session_id": "verify-coordinator-session",
        "origin_session_id": "verify-coordinator-session",
        "status": "sent",
        "detail": "verify_onboard 自检回执（占位，未投递真实会话）",
        "target_session_id": "verify-coordinator-session",
        "wake_kind": "unknown",
    })
    result = ctx.call("merge_ticket", {
        "project_id": ctx.project_id,
        "ticket_id": ctx.ticket_id,
        "commit": ctx.head_sha,
        "target_branch": ctx.target_branch,
        "tests": "verify_onboard 9/9",
        "reviewed_sha": ctx.head_sha,
    })
    if not result.get("merge_id"):
        raise VerifyError("merge_ticket 没有返回 merge_id")
    detail = ctx.call("get_ticket", {"project_id": ctx.project_id, "ticket_id": ctx.ticket_id})
    if len(detail.get("merges") or []) != 1:
        raise VerifyError("合并记录没有落库")
    return f"merge_id={result['merge_id'][:12]}… → {ctx.target_branch}"


CLOSING = """# 收尾包 R-001

## 1. 本轮原话目标

在临时 HOME 里跑完装 → 接项目 → 开单 → 领取 → 绑定 → 提交 → 验收 → 合并 → 收尾。

## 2. 已完成及证据

`python verify/verify_onboard.py` 逐行打印九步并通过，库里有 submission 与 merge 记录。

## 3. 未做到或偏离

无，因为这是临时工作区里的自检闭环，九步全部跑过；真实宿主会话不在本脚本范围内。

## 4. 决定不做及原因

不重复验证宿主会话与网络：自检只证明本机文件、库与闸门能串起来。

## 5. 欠 AI 的下一步

正式使用时把工作区加成宿主项目，并按 bootstrap-workspace 技能发第一条指令。

## 6. 欠人的一条可执行动作

打开 `verify/verify_onboard.py` 顶部注释，按其中的命令在目标机器上重跑一次。

## 7. 结论

已收尾
"""


def step_close(ctx: Context) -> str:
    round_file = ctx.ws / "tasks" / "round.md"
    text = round_file.read_text(encoding="utf-8")
    # 轮次号从 round.md 读，不写死一个：套件的轮次会往前走到 R-002、R-003，
    # 写死 R-001 的话收尾包会落在闸门找不到的位置（本脚本自己踩过一次）。
    found = re.search(r"^轮次[：:]\s*(R-\d{3})\s*$", text, flags=re.M)
    if found is None:
        raise VerifyError(f"{round_file} 里读不到「轮次：R-00N」这一行，收尾包不知道该落哪个号")
    round_no = found.group(1)
    text = re.sub(r"^状态[：:].*$", "状态：已收尾", text, count=1, flags=re.M)
    round_file.write_text(text, encoding="utf-8")
    (ctx.ws / "tasks" / f"closing-{round_no}.md").write_text(CLOSING, encoding="utf-8")
    result = run([sys.executable, "tools/close_gate.py"], cwd=ctx.ws, env=ctx.env, timeout=120)
    if result.returncode != 0:
        raise VerifyError(f"close_gate.py 退出 {result.returncode}：{(result.stdout + result.stderr)[-1200:]}")
    return f"收尾包 {round_no} 与状态位通过 close_gate"


RUNNERS = {
    "install": step_install,
    "add-project": step_add_project,
    "create_ticket": step_create_ticket,
    "claim": step_claim,
    "bind": step_bind,
    "submit": step_submit,
    "review": step_review,
    "merge": step_merge,
    "close": step_close,
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="在临时 HOME 里跑九步启动自检")
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    args = parser.parse_args(argv)

    base = Path(tempfile.mkdtemp(prefix="aiws-onboard-"))
    home = base / "home"
    home.mkdir()
    gitconfig = base / "gitconfig"
    gitconfig.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "verify onboard",
        "GIT_AUTHOR_EMAIL": "verify@example.invalid",
        "GIT_COMMITTER_NAME": "verify onboard",
        "GIT_COMMITTER_EMAIL": "verify@example.invalid",
        "PYTHONIOENCODING": "utf-8",
    }
    ctx = Context(base, env, args.keep)
    print(f"临时目录：{base}\n", flush=True)

    passed = 0
    try:
        for name in STEPS:
            print(f"[step] {name}", flush=True)
            try:
                detail = RUNNERS[name](ctx)
            except Exception as exc:  # noqa: BLE001 - 自检要把异常也变成一行可读的失败
                print(f"FAIL {name}：{exc}", flush=True)
                return 1
            print(f"PASS {name} — {detail}", flush=True)
            passed += 1
    finally:
        if args.keep:
            print(f"\n保留临时目录：{base}", flush=True)
        else:
            shutil.rmtree(base, ignore_errors=True)

    print(f"\n{passed}/{len(STEPS)} 步通过", flush=True)
    return 0 if passed == len(STEPS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
