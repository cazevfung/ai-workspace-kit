#!/usr/bin/env python
"""回执投递参数：把「发给谁、写什么」从库里的工单生成两段可直接复制的 JSON。

为什么要有这个脚本：`send_to_session` 不带 `target_session_id` 时会**新建一个任务**
（回执变成一条幽灵会话），而 `record_notice` 又不允许把投递目标写成执行会话自己。
这两条都靠人记得，记漏了当场没有反应——所以要有一个脚本，把参数从库里算出来，
人只需要复制。

    python tools/notice_payload.py --ticket AW-060
    python tools/notice_payload.py --ticket AW-060 --attempt <attempt_id>
    python tools/notice_payload.py --ticket AW-060 --status failed --detail "目标会话已归档"

打印两段：

1. `send_to_session` —— 发回执要用的 `{message, target_session_id}`；执行会话用它，
   带 `target_session_id` 才不会误建新任务。
2. `record_notice` —— 当前 owner 落库用的完整 payload（含 `target_session_id` 与
   `wake_kind`），照抄即可；`wake_kind` 用宿主回执里的实际值改，默认 `queued`。

收件人与落库人取这张单的 `tickets.owner_session_id`（现在负责收尾的那条会话）：
`record_notice` 的授权与 target 读的就是它，两边用同一个值才不会在接替之后报
`NOT_TARGET`（AW-122）。老库没有这一列时回退到该尝试 `claim_ticket` 时写下的
`coordinator_session_id`，两者都取不到时非零退出并说明原因，不猜一个字符串出来。

读的是**最新值**：库旁有非空 `-wal` 时按 `mode=ro` 读（会留 `-shm`/`-wal` 旁文件），
没有才用 `immutable=1`（一个旁文件都不留）；`immutable=1` 那条路开库后还会再核一次
`-wal`，开库那一刻才落地的也能读到（见 `read_only`）。每次输出第二行都写明这次走了哪条路。

退出码：0 打印两段 JSON；2 参数、库或数据不满足（原因写 stderr）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DB = ROOT / ".data" / "task-center.sqlite3"

# 中心库怎么发现只有一处：`tools/center_config.py`（三个闸门与两个巡检工具都从它取）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import center_config  # noqa: E402


def default_ticket_db():
    """默认工单库：与三个闸门同一处发现（`tools/center_config.py`）。

    顺序：工作区指针 `.agents/center.json` 记的 agents-home → 本检出的
    `.data/task-center.sqlite3` → 主检出（工作树里按 git 公共目录）→
    `~/.agents/center.json`。在 `.worktrees/<工单号>/` 里跑时本树没有 `.data`
    （不入库），按 `ROOT/.data` 取会报「中心库不存在」——那不是「这张单没有收件人」，
    是找错了库，而 C1 给的那条命令本来就不带 `--db`。
    一条都不中时返回本检出的默认位置，让调用方照常报「库不存在」。
    """
    try:
        return center_config.resolve(repo=ROOT).db
    except center_config.DiscoveryError:
        return DEFAULT_DB


class PayloadError(RuntimeError):
    """打印不出参数时的唯一异常：带一句人能读的原因，由 main 打到 stderr。"""


def _wal_bytes(path):
    """库旁 `-wal` 有多少字节；看不清（正在 checkpoint / 没权限）就当是 0。

    单独拿出来是为了让「开库前查一次、开库后再查一次」共用同一道判断，
    也为了让窗口能被用例复现（AW-064 C9）。
    """
    try:
        wal = Path(path).with_name(Path(path).name + "-wal")
        return wal.stat().st_size if wal.is_file() else 0
    except OSError:
        return 0


def _open_read_only(path, wal_size):
    """按已知的 `-wal` 大小选路开一条只读连接，返回 `(conn, how)`。"""
    if wal_size:
        uri = Path(path).as_uri() + "?mode=ro"
        how = (f"mode=ro：库旁有 {wal_size} 字节未 checkpoint 的 -wal，"
               "immutable=1 会跳过它读到旧值；这次宁可留 -shm/-wal 旁文件也要读新值")
    else:
        uri = Path(path).as_uri() + "?mode=ro&immutable=1"
        how = "immutable=1：库旁没有非空 -wal，跑完不会在库旁留下 -shm/-wal"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn, how


def read_only(path):
    """只读连接：不建库、不写一个字节。返回 `(conn, how)`，`how` 说明走的是哪条路。

    两条路，按库旁**有没有非空** `<库>-wal` 选：

    - 没有非空 `-wal`：`mode=ro&immutable=1`。它不读 WAL、不加锁、也不建
      `-shm`/`-wal`，所以跑完库旁一个文件都不多。
    - 有非空 `-wal`：退回 `?mode=ro`（只读，但 SQLite 要建 `<库>-shm` 才能读 WAL）。
      **这是有意的取舍，不是退步**：`immutable=1` 等于告诉 SQLite「这个文件没人写过」，
      于是它跳过 WAL，读到的是 checkpoint 之前的旧值——AW-062 实测过，`-wal` 里
      4152 字节未 checkpoint 时，`immutable=1` 读到旧 attempt 与旧 state，`mode=ro`
      读到新值。旧值不带任何提示，比库旁多两个旁文件危险得多：宁可留旁文件，
      也要读到新值。

    **查一次不够（AW-064 C9）。** `stat` 与 `connect` 之间有一条真实的窗口：验收实测
    过 stat 时没有 `-wal`、开库前 156592 字节落地，于是工具 exit=0 却输出旧 state 与
    旧 attempt，而读取方式那行照写 `immutable=1`——静默读旧值。所以走 `immutable=1`
    之后要再看一次：非空就关掉重开 `mode=ro`。反方向的窗口（选了 `mode=ro` 而 `-wal`
    刚好被 checkpoint 掉）不用处理：那时主库文件已经是新的，`mode=ro` 读到的就是新值。

    `PRAGMA query_only=ON` 两条路都加：即使将来有人把 URI 改错成可写，这条连接也
    写不进去。

    另外，旧说明写着「版本不匹配照样能查」：它当时走 `Center(...)`，而
    `task_center.db.connection()` 是 `mode=rw` 开库、先比 `PRAGMA user_version` 的，
    v7 库上直接 `SCHEMA_MISMATCH`。现在整段不经过 `Center`（见 `read_ticket`）。
    """
    # as_uri() 不接相对路径：给了相对的 --db 就会抱 ValueError 而不是退出码 2。
    path = Path(path).absolute()
    if not path.is_file():
        raise PayloadError(f"中心库不存在：{path}")
    wal_size = _wal_bytes(path)
    conn, how = _open_read_only(path, wal_size)
    if not wal_size:
        late = _wal_bytes(path)
        if late:
            conn.close()
            conn, how = _open_read_only(path, late)
            how = (f"mode=ro：开库前库旁没有非空 -wal，开库时已有 {late} 字节落地"
                   "（stat 与 connect 之间有窗口），已关掉重开读新值；"
                   "这次宁可留 -shm/-wal 旁文件")
    return conn, how


def read_ticket(conn, ticket_id, project_id=None):
    """取回 `get_ticket` 里这个脚本用到的那几段（只读）。

    为什么不直接调 `get_ticket`：那条路要经 `Center`，而 `Center` 以读写方式开库。
    这个脚本只生成两段给人复制的 JSON，不该为了读一次元数据就在真库旁边留下
    `-shm` 与 `-wal`。所以只查用到的那几列：工单行（含 `project_id` 与
    `current_attempt`）、尝试列表、最后一个提交号、第一条合并提交号。

    读取路径由 `read_only` 选：库旁没有非空 `-wal` 时不留下任何旁文件；有则宁可
    留下旁文件也要读到最新值（理由在 `read_only`）。
    """
    row = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if row is None:
        raise PayloadError(f"工单不存在：{ticket_id}")
    ticket = dict(row)
    if project_id and project_id != ticket["project_id"]:
        raise PayloadError(f"工单 {ticket_id} 属于项目 {ticket['project_id']}，不是 {project_id}")
    ticket["attempts"] = [dict(item) for item in conn.execute(
        "SELECT * FROM attempts WHERE ticket_id=? ORDER BY rowid", (ticket_id,))]
    # `head_sha` 不是这个脚本要的，是 `tools/receipt_payload.py` 要的：回执正文里的
    # 「提交 sha」取自最新一条提交。两个脚本共用这一条只读读法，不再各写一份取数。
    ticket["submissions"] = [dict(item) for item in conn.execute(
        "SELECT s.id, s.head_sha FROM submissions s JOIN attempts a ON a.id=s.attempt_id"
        " WHERE a.ticket_id=? ORDER BY s.rowid", (ticket_id,))]
    ticket["merges"] = [dict(item) for item in conn.execute(
        "SELECT commit_sha, target_branch FROM merges WHERE ticket_id=? ORDER BY rowid", (ticket_id,))]
    return ticket


def ticket_scope(db_path, ticket_id, project_id):
    """从库里取工单：`get_ticket` 要 project_id，而人只记得工单号。

    一次只读连接就够：工单行里有 `project_id` 与 `current_attempt`，尝试、提交与
    合并记录按工单号取。不再先跑一个 `Center`，见 `read_ticket` 的说明。
    """
    conn, how = read_only(db_path)
    try:
        return read_ticket(conn, ticket_id, project_id), how
    finally:
        conn.close()


def pick_attempt(ticket, wanted):
    """没指定就取当前尝试；都没有尝试时说明「这张单还没派发过」。"""
    attempts = ticket["attempts"]
    if not attempts:
        raise PayloadError(f"工单 {ticket['id']} 还没有任何执行尝试：先 claim_ticket 再取参数")
    if wanted:
        for attempt in attempts:
            if attempt["id"] == wanted:
                return attempt
        raise PayloadError(f"尝试 {wanted} 不属于工单 {ticket['id']}")
    current = ticket.get("current_attempt")
    for attempt in attempts:
        if attempt["id"] == current:
            return attempt
    return attempts[-1]


def coordinator_of(attempt):
    coordinator = attempt.get("coordinator_session_id")
    if not coordinator:
        raise PayloadError(
            f"尝试 {attempt['id']} 没有记录 coordinator_session_id：claim_ticket 时没写，"
            "回执没有确定的目标；先补记一次 claim 再取参数（不猜一个字符串出来）"
        )
    return coordinator


def recipient_of(ticket, attempt):
    """
    收件人 = 这张单现在的 owner（`tickets.owner_session_id`），没有才回退 origin。

    `record_notice` 的授权与 target 都读 owner（AW-114），而**回执该发给谁**以前打印的是
    origin。两者在 `take_over_ticket` 之后就不相等了：执行会话把消息发到已经归档的 origin，
    owner 这边就永远落不下 `sent`（`NOT_TARGET`）。所以这里与 `receipt_payload` 用同一个值
    ——「发给谁」与「谁有权记」是同一条会话（AW-122）。
    """
    owner = ticket.get("owner_session_id")
    if owner:
        return owner
    return coordinator_of(attempt)


def message_for(ticket, attempt):
    """回执正文：只写库里查得到的事实，结论由发信人自己补。"""
    parts = [f"{ticket['id']} 状态 {ticket['state']} / revision {ticket['revision']}",
             f"attempt {attempt['id']}",
             f"项目 {ticket['project_id']}"]
    if ticket["submissions"]:
        parts.append(f"提交 {ticket['submissions'][-1]['id']}")
    if ticket["merges"]:
        parts.append(f"合并 {ticket['merges'][0]['commit_sha'][:12]}")
    parts.append("回执请带 target_session_id 发回当前 owner（tickets.owner_session_id），不要省略它")
    return " / ".join(parts)


def block(name, payload):
    return [f"--- {name} ---", json.dumps(payload, ensure_ascii=False, indent=2)]


def render(ticket, attempt, status, detail, origin):
    coordinator = recipient_of(ticket, attempt)
    send = {"message": message_for(ticket, attempt), "target_session_id": coordinator}
    notice = {
        "project_id": ticket["project_id"],
        "ticket_id": ticket["id"],
        "attempt_id": attempt["id"],
        "session_id": coordinator,
        "origin_session_id": origin or coordinator,
        "status": status,
        "detail": detail or f"已把回执发回原会话（{ticket['id']}）",
        "target_session_id": coordinator,
        # 占位值就是最常见的那个；真实取值只能照宿主回执里的 wake_kind 填，见下方说明。
        "wake_kind": "queued",
    }
    lines = []
    lines += block("send_to_session", send)
    lines += block("record_notice", notice)
    lines.append("说明：wake_kind 照抄宿主回执里的原值（queued / resumed / already-active 等）；"
                 "只有 created 表示宿主把它当成新建任务了，那一条要记 failed。")
    lines.append("说明：收件人与落库人都是这张单现在的 owner（tickets.owner_session_id）；"
                 "origin 只用于事后追查谁派的单，不再参与投递。")
    return lines


def main(argv=None):
    try:  # Windows 控制台默认 GBK，中文说明重定向就变乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="从中心库生成回执投递参数（只读）。")
    parser.add_argument("--ticket", required=True, help="工单号，例如 AW-060")
    parser.add_argument("--attempt", default=None, help="尝试编号；默认取当前尝试")
    parser.add_argument("--project", default=None, help="项目标签；默认按工单号反查")
    parser.add_argument("--db", default=None,
                        help="中心库路径（默认主检出那份；在 .worktrees/<工单号>/ 里跑也能找到）")
    parser.add_argument("--status", default="sent", choices=("sent", "failed"), help="回执结果，默认 sent")
    parser.add_argument("--detail", default=None, help="回执措辞；失败时写原因")
    parser.add_argument("--origin", default=None, help="回执的收件会话；默认就是协调会话")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else default_ticket_db()
    try:
        if args.status == "failed" and not args.detail:
            raise PayloadError("--status failed 需要 --detail 写清失败原因，空原因事后查不出发生了什么")
        ticket, how = ticket_scope(db_path, args.ticket, args.project)
        attempt = pick_attempt(ticket, args.attempt)
        # render 里还要取协调会话标识，取不到同样只报一句原因，不抛栈。
        lines = render(ticket, attempt, args.status, args.detail, args.origin)
    except PayloadError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    print(f"# {ticket['id']}：复制下面两段的 JSON")
    print(f"# 读取方式：{how}")
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
