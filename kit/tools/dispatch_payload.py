#!/usr/bin/env python
"""派单消息的骨架：按工单号打印「库里能知道的那五行」（只读，不改库）。

**它不打印整条派单消息。** 库里只有标题、交付物、判据，生成不出「上一轮抠出来的
icon 不能用、这次不要贴 PSD」这种现场；把整条消息交给机器，等于把执行会话的上下文
抽干，那比回执漏发更贵。所以分工是死的：

- **骨架段（机器写）**：工单与项目根 / 协调会话 id（取 owner）/ 开工第一步 /
  开工方式（git 或 non_git）/ 回执怎么发。这五行只写「重读一次库仍然成立」的东西，
  `attempt_id`、commit sha、行号、`revision` 一律不出现——它们几小时内就过期。
- **现场段（人写）**：黄金圈 Why / How / What + 「最容易做歪的地方」，由协调会话
  写成一份文件、用 `--context` 传进来，工具把它**原样接在骨架段后面**，中间一条
  分隔线。派单方写什么，执行会话就看到什么，工具一个字都不改、也不润色。

为什么只校验、不生成现场段：机器能判的只有「在不在、空不空、是不是纯指针」，
判不了 Why 写得好不好——那句「Why 必须能改变执行中的判断」是验收会话判的
（判据 C4 就是拦「生成器保证 Why 有效」这种话）。

    python tools/dispatch_payload.py --ticket AW-111 --context .data/untracked/AW-111/context.md
    python tools/dispatch_payload.py --ticket AW-111 --context <文件> --json   # 给机器读

stdout 的形状（骨架段五行 + 分隔线 + 现场段原文）：

    工单：AW-111（project_id=ai-workspace，项目根 {{WORKSPACE}}）
    协调会话 id：…（tickets.owner_session_id 的原值，缺了执行会话没处回执）
    开工第一步：用 get_ticket 取当前 attempt 与判据（criteria 已到 v2，以库为准）；再 bind_session
    开工方式：项目根是 git 仓库 → git worktree add .worktrees/AW-111 -b ticket/AW-111 master……
    回执怎么发：python tools/receipt_payload.py --ticket AW-111 打印 send_to_session 的完整 args
    --- 现场段（派单人写的，不是库里的字段） ---
    Why：…
    How：…
    What：…
    最容易做歪的地方：…

取数与 `tools/receipt_payload.py` 是**同一套**：同一个库（`default_ticket_db`：本树
`.data`，在 `.worktrees/<工单号>/` 里跑则按 git 公共目录找主检出）、同一条只读连接
（`notice_payload.read_only`，不写一个字节）、同一张单、收件人取同一列
`tickets.owner_session_id`（不是 `attempts.coordinator_session_id`，那是 origin，只作
审计）。协调会话凭什么信这条命令?就凭两个工具读同一列——`record_notice` 的授权与
target 也是这一列。

**任一条件不满足时 stdout 一个字节都不打印**（没有 `--context`、文件不存在、内容为空、
缺 Why / How / What / 最容易做歪的地方 任一节、某节正文为空、Why 是纯指针、owner 为空、
没有尝试、状态不在 `dispatching` / `running`）：打印到一半再报错，半条派单消息就会被
贴出去，那正是 NC-007 的同一种失效。原因只往 stderr 写，且点名是哪一节。

退出码：0 打印骨架段 + 现场段；2 参数、库或内容不满足（原因写 stderr）。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from notice_payload import (
    PayloadError,
    default_ticket_db,
    pick_attempt,
    read_only,
    read_ticket,
)

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DB = ROOT / ".data" / "task-center.sqlite3"

# 只有这两个状态是「可以派出去」的：`dispatching` 是刚 claim、还没绑会话，
# `running` 是绑过会话在跑（recover 之后重发也走这两个）。别的状态派出去，
# 执行会话第一步 `bind_session` 就会被拒。
DISPATCHABLE_STATES = ("dispatching", "running")

# 现场段的四节。前三节是黄金圈，第四节是派单前三条质询的落点。
# key 用英文小写，报错时用中文标签点名（「缺『最容易做歪的地方』一节」）。
CONTEXT_LABELS = (
    ("why", ("why", "为什么")),
    ("how", ("how", "怎么做")),
    ("what", ("what", "交付物")),
    ("pitfall", ("最容易做歪的地方", "最容易做歪", "pitfall")),
)
REQUIRED_SECTIONS = ("why", "how", "what", "pitfall")
SECTION_NAMES = {"why": "Why", "how": "How", "what": "What",
                 "pitfall": "最容易做歪的地方"}

# 整段只写指向的几种开头：`见 PRD` / `详见工单` / `参考 docs/x.md` / `同上`……
POINTER_PREFIX = re.compile(r"^(详见|参见|参考|阅读|见下|见上|同上|如上|按|见)")
# 剩下那截允许出现的「指向对象」：编号、路径、章节号、`PRD`/`工单`/`文档` 这类名词。
REFERENCE_WORDS = {
    "prd", "工单", "文件", "文档", "上文", "下文", "附件", "本文", "本单",
    "那张单", "这张单", "以上", "上面", "下面", "节", "章", "段", "页码", "链接",
    "描述", "交付物", "判据", "第一条", "全部",
}


def is_pure_pointer(text):
    """Why 这一节是不是「整段只写指针」（例如就一句「见 PRD」）。

    判据只有一句：去掉指向动词之后，剩下的每一个词都只是「指向对象」。机器判不了
    这句话有没有用，只判它是不是一句自己的话都没有——纯指针的 Why 等于没写，因为
    执行会话据此拿不到任何能改变判断的信息。非纯指针一律放行（宁松不误杀）。
    """
    body = (text or "").strip().strip("。.；;，,、 　")
    if not body:
        return True
    if len(body) > 60:  # 一整段话不会是纯指针（指针通常很短）
        return False
    match = POINTER_PREFIX.match(body)
    if not match:
        return False
    rest = body[match.end():].strip()
    if not rest:
        return True
    tokens = [tok for tok in re.split(r"[\s，,、和与的「」『』《》():：·\-—]+", rest) if tok]
    if not tokens:
        return True
    for tok in tokens:
        low = tok.lower()
        if low in REFERENCE_WORDS:
            continue
        if any(ch.isdigit() for ch in low):  # PRD-001 / §3 / 第 2 节 / aw168-context.md
            continue
        if re.search(r"\.(md|py|json|txt|docx?|pdf)$", low):
            continue
        return False
    return True


def parse_context(raw):
    """把现场段按四个标签切成四节，返回 `{key: 正文}`。

    容忍几种常见写法：标签行前面带 `#` 或 `-`、`Why：` 与 `Why:` 都收、标签单独一行
    或标签后直接跟正文。标签之前的零散内容不参与判据（它不是四节之一，工具不替人
    决定那段算什么），但整份内容是否为空由调用方先判。
    """
    lines = raw.splitlines()
    sections = {}
    current = None
    for line in lines:
        stripped = line.strip().lstrip("#-*+> \t").strip()
        match = re.match(r"^([^:：]{1,20})[:：]\s*(.*)$", stripped)
        if match:
            label, body = match.group(1).strip(), match.group(2).strip()
            key = None
            for candidate, aliases in CONTEXT_LABELS:
                if label.lower() in aliases or any(label.lower().startswith(alias) for alias in aliases):
                    key = candidate
                    break
            if key:
                current = key
                sections.setdefault(key, [])
                if body:
                    sections[key].append(body)
                continue
        if current:
            sections[current].append(line.rstrip())
    return {key: "\n".join(parts).strip() for key, parts in sections.items()}


def check_context(raw):
    """现场段的四道校验；任一不过就抛 `PayloadError`，把缺的节名一次点全。"""
    if not (raw or "").strip():
        raise PayloadError(
            "--context 文件是空的：现场段（Why / How / What / 最容易做歪的地方）不能为空，"
            "库里生成不出这一单的现场。把黄金圈写进文件再跑"
        )
    sections = parse_context(raw)
    problems = []
    for key in REQUIRED_SECTIONS:
        name = SECTION_NAMES[key]
        body = sections.get(key, "")
        if key not in sections:
            problems.append(f"缺「{name}」一节")
        elif not body.strip():
            problems.append(f"「{name}」一节正文为空")
        elif key == "why" and is_pure_pointer(body):
            problems.append(
                f"「Why」是纯指针（整段只有「{body.strip()}」这类指向）——"
                "Why 要写这一单为什么这么做、1–2 句，能改变执行中的判断"
            )
    if problems:
        raise PayloadError(
            "--context 不满足派单消息的现场段：" + "；".join(problems)
            + "。一条消息都不打印——半条消息被贴出去比没有更贵"
        )
    return sections


def project_root_of(conn, project_id):
    """项目根：只从 `projects.root` 取，不读环境变量、不从工单号猜。

    取不到就是取不到——派单消息里的「开工方式」全靠这个目录是不是 git 仓库，
    猜一个路径进去，执行会话会在一棵不存在的树里开工。
    """
    row = conn.execute("SELECT root FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None or not (row["root"] or "").strip():
        raise PayloadError(
            f"项目 {project_id} 在库里没有登记根目录（projects.root 为空或没有这一行）："
            "派单消息里的「开工方式」无从判断。先 register_project 或 relocate_project，"
            "不要凭印象写一个路径"
        )
    return row["root"]


def repo_kind_of(root):
    """项目根是不是 git 仓库：跑一条只读的 git 命令，按退出码判，不看目录名。

    非 git 项目（例如内容型业务项目）不是错——它们的提交走
    `submit_result` 的 `repo_kind="non_git"`，两棵树都不传（AW-151）。所以这里
    给的是两条不同的开工方式，不是一条成功一条失败。
    """
    try:
        run = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-dir"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "non_git"
    return "git" if run.returncode == 0 else "non_git"


def render_skeleton(ticket, project_root, repo_kind):
    """骨架段五行：工单 / 协调会话 id / 开工第一步 / 开工方式 / 回执怎么发。

    每一行都只写「重读一次库仍然成立」的东西；当前是哪一条 attempt 不进正文，
    只说「以 get_ticket 的当前 attempt 为准」——那个 id 几小时内就会过期。
    """
    ticket_id = ticket["id"]
    # 统一的路径写法：库里存的是 Windows 反斜杠，消息里给正斜杠（与本仓库文档一致，
    # 也让同一条消息在 PowerShell 与 bash 里都能原样粘贴）。
    shown_root = str(project_root).replace("\\", "/")
    if repo_kind == "git":
        how = (f"开工方式：项目根是 git 仓库 → git worktree add .worktrees/{ticket_id} "
               f"-b ticket/{ticket_id} master，之后只在这棵树里改")
    else:
        how = ("开工方式：项目根不是 git 仓库 → submit_result 传 repo_kind=non_git，"
               "head_sha/base_sha 都不传")
    return [
        f"工单：{ticket_id}（project_id={ticket['project_id']}，项目根 {shown_root}）",
        f"协调会话 id：{ticket['owner_session_id']}"
        "（tickets.owner_session_id 的原值，缺了执行会话没处回执）",
        f"开工第一步：用 get_ticket 取当前 attempt 与判据"
        f"（criteria 已到 v{ticket['criteria_version']}，以库为准）；再 bind_session"
        f"（project_root 写 {shown_root}）",
        how,
        f"回执怎么发：python tools/receipt_payload.py --ticket {ticket_id} "
        f"打印 send_to_session 的完整 args；target_session_id 不能省，"
        f"send_to_session 要经 cindy_helper 的 call_tool 调",
    ]


CONTEXT_SEPARATOR = "--- 现场段（派单人写的，不是库里的字段） ---"


def render(ticket, project_root, repo_kind, raw_context):
    """骨架段 + 分隔线 + 现场段原文；现场段一个字都不改。"""
    skeleton = render_skeleton(ticket, project_root, repo_kind)
    return skeleton + [CONTEXT_SEPARATOR, raw_context.rstrip("\n")]


def build(db_path, ticket_id, project_id, context_path):
    """把骨架段与现场段算出来；任何一处不满足都抛 `PayloadError`，不返回半条。"""
    if not ticket_id:
        raise PayloadError("必须先给工单号：--ticket <单号>")
    if not context_path:
        raise PayloadError(
            "缺 --context：现场段（Why / How / What / 最容易做歪的地方）必须由派单人写成"
            "文件传进来，库里生成不出这一单的现场；缺了它就不许发派单消息，一条都不打印"
        )
    path = Path(context_path)
    if not path.is_file():
        raise PayloadError(
            f"--context 文件不存在：{context_path}。现场段（Why / How / What / 最容易做歪的地方）"
            "必须由派单人写成文件传进来：库里生成不出这一单的现场，缺了它就不许发派单消息"
        )
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise PayloadError(f"--context 文件读不出来：{context_path}（{exc}）") from exc
    sections = check_context(raw)

    conn, how = read_only(db_path)
    try:
        ticket = read_ticket(conn, ticket_id, project_id)
        project_root = project_root_of(conn, ticket["project_id"])
    finally:
        conn.close()

    attempt = pick_attempt(ticket, None)
    owner = (ticket.get("owner_session_id") or "").strip()
    if not owner:
        raise PayloadError(
            f"工单 {ticket_id} 没有 owner（tickets.owner_session_id 为空）："
            "派单消息里「协调会话 id」那一行就无从写起，执行会话做完也没有确定的收件人。"
            "先 claim_ticket（它把 coordinator_session_id 同时写成 owner）或 "
            "take_over_ticket 接一条在跑的会话，再打印这条消息"
        )
    if ticket["state"] not in DISPATCHABLE_STATES:
        raise PayloadError(
            f"工单 {ticket_id} 当前状态是 {ticket['state']}，不在 "
            f"{'/'.join(DISPATCHABLE_STATES)} 之内：这个状态派出去，执行会话第一步 "
            "bind_session 就会被拒。先 claim_ticket 再打印"
        )

    repo_kind = repo_kind_of(project_root)
    lines = render(ticket, project_root, repo_kind, raw)
    return {
        "ticket": ticket_id,
        "project_id": ticket["project_id"],
        "project_root": project_root,
        "owner_session_id": owner,
        "coordinator_session_id": attempt.get("coordinator_session_id"),
        "criteria_version": ticket["criteria_version"],
        "state": ticket["state"],
        "repo_kind": repo_kind,
        "skeleton": render_skeleton(ticket, project_root, repo_kind),
        "context_path": str(context_path),
        "sections": sections,
        "lines": lines,
        "read_mode": how,
    }


def main(argv=None):
    try:  # Windows 控制台默认 GBK，中文说明重定向就变乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(
        description="从中心库生成派单消息的骨架段，接上 --context 的现场段（只读）。")
    parser.add_argument("--ticket", required=True, help="工单号，例如 AW-111")
    # 不用 argparse 的 required：它走的是 SystemExit，报错内容混着用法说明，
    # 而这条工具的规矩是「不满足就只往 stderr 写一句原因、stdout 一个字节都没有」。
    parser.add_argument("--context", default=None,
                        help="现场段文件（Why / How / What / 最容易做歪的地方 各一节）")
    parser.add_argument("--project", default=None, help="项目标签；默认按工单号反查")
    parser.add_argument("--db", default=None,
                        help="中心库路径（默认主检出那份；在 .worktrees/<工单号>/ 里跑也能找到）")
    parser.add_argument("--json", action="store_true", help="输出一段 JSON，给机器读")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else default_ticket_db()
    try:
        payload = build(db_path, args.ticket, args.project, args.context)
    except PayloadError as exc:
        # 只写 stderr：stdout 空着，才不会有人把半条消息贴出去。
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for line in payload["lines"]:
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
