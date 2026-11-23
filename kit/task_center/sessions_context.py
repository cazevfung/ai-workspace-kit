"""一张单牵扯到的会话与消息位置（PRD-015 R5）。

放在 `core.py` 外面是为了不挪动那里已被文档按行号引用的代码：这三件事各回答一个问题——
链接怎么拼（`session_url`）、会话从哪几列来（`sessions_for`）、消息 id 收什么形状（`message_id`），
而工单状态机不需要知道其中任何一件。
"""
from .db import require
from .hostsessions import host_sessions


def session_url(session_id, device):
    """拼一条宿主认识的会话链接（PRD-015 R5）。

    `device` 有值才加 `?device=` 参数：本机 `sessions.agent_device_id` 全是 NULL，
    拼一个假 device 会得到一条点开落不到东西的链接（验收条件 C3 的反例）。
    """
    base = f"cindy://session/{session_id}"
    return f"{base}?device={device}" if device else base


def sessions_for(conn, ticket_id, host=None):
    """一张单牵扯到的会话，按 [请求方 → 协调 → 执行 → 验收 → 回执收件人] 顺序。

    只答库里真有的东西：空会话标识不产出条目（不生成 `cindy://session/` 这种死链接），
    `device` 与 `session_state` 来自宿主库的只读查询，宿主库读不到时是 `null` / `unknown`。
    请求方是一列独立的记录（`tickets.requester_session_id`），不拿协调会话冒充。
    """
    directory = host_sessions() if host is None else host
    ticket = conn.execute("SELECT requester_session_id, created_at FROM tickets WHERE id=?",
                          (ticket_id,)).fetchone()
    items = []

    def add(role, raw, at, source, message_id=None):
        if not isinstance(raw, str) or not raw.strip():
            return
        session_id = raw.strip()
        info = directory.get(session_id) or {}
        device = info.get("device") or None
        state = info.get("state") or "unknown"
        item = {
            "role": role,
            "session_id": session_id,
            "session_url": session_url(session_id, device),
            "device": device,
            "session_state": state,
            # 归档/删除的链接照给，但要说它是旧的；未知不等于已失效，所以只认这两档。
            "stale": state in ("archived", "deleted"),
            "at": at,
            "source": source,
        }
        if role == "receipt_target":
            item["message_id"] = message_id
        items.append(item)

    if ticket is not None:
        add("requester", ticket["requester_session_id"], ticket["created_at"],
            "tickets.requester_session_id")
    for row in conn.execute("SELECT coordinator_session_id, session_id, created_at FROM attempts"
                            " WHERE ticket_id=? ORDER BY rowid", (ticket_id,)):
        add("coordinator", row["coordinator_session_id"], row["created_at"],
            "attempts.coordinator_session_id")
        add("executor", row["session_id"], row["created_at"], "attempts.session_id")
    for row in conn.execute(
            "SELECT r.reviewer_session_id, r.created_at FROM reviews r"
            " JOIN submissions s ON s.id=r.submission_id JOIN attempts a ON a.id=s.attempt_id"
            " WHERE a.ticket_id=? ORDER BY r.rowid", (ticket_id,)):
        add("reviewer", row["reviewer_session_id"], row["created_at"], "reviews.reviewer_session_id")
    for row in conn.execute("SELECT target_session_id, message_id, created_at FROM notices"
                            " WHERE ticket_id=? ORDER BY rowid", (ticket_id,)):
        add("receipt_target", row["target_session_id"], row["created_at"],
            "notices.target_session_id", row["message_id"])
    return items


def message_id(data, max_length=500):
    """消息 id 只存 id 本身：一个 token，不收正文、不收链接（PRD-015 R5）。

    形状校验与 `wake_kind` 同一口径：非空、无空白、不超长。不在这里猜一个值：
    调用方拿不到 id 就不传，库里留 NULL，比拿时间戳或标题凑一个更接近事实。
    """
    value = data.get("message_id")
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= max_length,
            "INVALID_INPUT", f"message_id must be a nonempty string of at most {max_length} characters")
    require(not any(character.isspace() for character in value), "INVALID_INPUT",
            "message_id must be one token without internal whitespace or newlines")
    return value
