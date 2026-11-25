"""不变量自检：库里的 attempts 与 revision 必须能在 events 里找到来源（AW-159）。

为什么需要它：`Center.execute()` 每写成功一次就写一条带 `result` 的事件，所以
「库里多了一行、events 里却查不到是谁写的」这件事只有一条成因——**有人绕过了工具，
直接往 SQLite 里写**。工具自己拦不住那一类写入（那是文件权限层面的事，而这一层在
本机是同一个用户），能守的是两件当场可判的事：

1. **每条 attempt 都要有一条 `claim_ticket` 事件指着它。** 尝试只有 `_claim_ticket`
   一处会插入（`INSERT INTO attempts` 全仓唯一），所以「有尝试、没有 claim 事件」
   就一定是手写的行。2026-11-24 HSE-005 的 `ae87a2fc` 正是这种行：14:04:57 取消，
   14:28:38 出现一条 `revision=3` 的尝试，`events` 里没有对应记录，而随后 14:30:56
   的 `submit_result` **成功了**——因为手写那一步顺手把工单放回了 `running`。

2. **工单的 `revision` 必须等于事件里出现过的最大 revision。** `revision` 只在
   `_invalidate()` 里加一（`UPDATE tickets SET revision=revision+1` 全仓唯一），而它的
   三个调用方（`cancel_ticket` / `recover_ticket` / `revise_ticket restart`）都把新的
   revision 写进回执、回执又原样进 `events.detail.result`。所以「revision 比事件里
   的任何一个都大」= 有人无事件地推高了它。

3. **`submissions.artifacts` 的每一项恰好三个键。** 这一条不是「绕过工具」的判据，
   而是同一类毛病的落点：HSE-008 的 `1cd60777` 多了 `role`，验收每次停在
   `ARTIFACT_CHANGED`。`submit_result` 产不出那种行，所以它也会被这条点名。

自检**只报不改**：可疑行是事实，修法是先看那一行、判断它是怎么来的，再由人决定
补一条 `recover_ticket` 还是把它记成历史。工具在这里不删行、不改状态。
"""
from __future__ import annotations

import json

from .artifacts import ARTIFACT_KEYS

# 三类的名字就是回执里的 `check` 字段，读侧按它分档，不按顺序。
ATTEMPT_WITHOUT_CLAIM = "attempt_without_claim"
REVISION_WITHOUT_EVENT = "revision_without_event"
ARTIFACT_KEY_SHAPE = "artifact_key_shape"

CHECKS = (ATTEMPT_WITHOUT_CLAIM, REVISION_WITHOUT_EVENT, ARTIFACT_KEY_SHAPE)

# 每一类查什么，写进回执——读的人不必打开这个文件也知道这条在问哪件事。
MEANING = {
    ATTEMPT_WITHOUT_CLAIM:
        "每条 attempts 行都要有一条 claim_ticket 事件指着它（尝试只由 _claim_ticket 插入）",
    REVISION_WITHOUT_EVENT:
        "工单的 revision 必须等于它事件里出现过的最大 revision（revision 只在 _invalidate 里加一）",
    ARTIFACT_KEY_SHAPE:
        "submissions.artifacts 的每一项必须恰好含 " + " / ".join(ARTIFACT_KEYS) + " 三键",
}


def _detail(raw):
    """event.detail 是 JSON 文本；读不出来就当空字典，别让一行坏数据掀掉整次自检。"""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def claim_events(conn):
    """`attempt_id -> 宣告它的那条 claim_ticket 事件的 id`。

    只认成功事件：被拒的 claim 写的是 `{"rejected": ...}`，没有 `result.attempt_id`，
    拿它当来源等于把「试过」当成「写成了」。
    """
    found = {}
    for row in conn.execute(
            "SELECT id, ticket_id, detail FROM events WHERE operation='claim_ticket' ORDER BY id"):
        attempt_id = (_detail(row["detail"]).get("result") or {}).get("attempt_id")
        if isinstance(attempt_id, str) and attempt_id:
            found.setdefault(attempt_id, {"event_id": row["id"], "ticket_id": row["ticket_id"]})
    return found


def revision_trail(conn):
    """`ticket_id -> (最大 revision, 说出这个数的操作名, 那条事件的 created_at)`。"""
    trail = {}
    for row in conn.execute(
            "SELECT operation, ticket_id, detail, created_at FROM events"
            " WHERE ticket_id IS NOT NULL ORDER BY created_at, id"):
        revision = (_detail(row["detail"]).get("result") or {}).get("revision")
        if type(revision) is not int:
            continue
        seen = trail.get(row["ticket_id"])
        if seen is None or revision > seen[0]:
            trail[row["ticket_id"]] = (revision, row["operation"], row["created_at"])
    return trail


def attempts_without_claim(conn, project_id=None):
    """有尝试、events 里却没有一条 claim_ticket 指着它。这是手写行的判据。"""
    claimed = claim_events(conn)
    rows = conn.execute(
        "SELECT a.id, a.ticket_id, a.revision, a.state, a.session_id, a.created_at"
        " FROM attempts a JOIN tickets t ON t.id=a.ticket_id"
        + (" WHERE t.project_id=?" if project_id else "")
        + " ORDER BY a.created_at, a.rowid",
        (project_id,) if project_id else ()).fetchall()
    return [
        {"check": ATTEMPT_WITHOUT_CLAIM, "ticket_id": row["ticket_id"], "attempt_id": row["id"],
         "attempt_revision": row["revision"], "attempt_state": row["state"],
         "session_id": row["session_id"], "created_at": row["created_at"],
         "detail": "没有 claim_ticket 事件指着这条尝试：它不是 claim 写的，是绕过工具直接落库的行"}
        for row in rows if row["id"] not in claimed
    ]


def revisions_without_event(conn, project_id=None):
    """工单的 revision 比事件里出现过的任何一个都大（或根本没有带 revision 的事件）。"""
    trail = revision_trail(conn)
    rows = conn.execute(
        "SELECT id, project_id, revision, state, created_at FROM tickets"
        + (" WHERE project_id=?" if project_id else "")
        + " ORDER BY created_at, rowid",
        (project_id,) if project_id else ()).fetchall()
    findings = []
    for row in rows:
        seen = trail.get(row["id"])
        if seen is None:
            findings.append({
                "check": REVISION_WITHOUT_EVENT, "ticket_id": row["id"],
                "ticket_revision": row["revision"], "ticket_state": row["state"],
                "created_at": row["created_at"],
                "detail": "这张单没有任何带 revision 的事件，连建单那条都没有"})
        elif seen[0] != row["revision"]:
            findings.append({
                "check": REVISION_WITHOUT_EVENT, "ticket_id": row["id"],
                "ticket_revision": row["revision"], "ticket_state": row["state"],
                "event_revision": seen[0], "event_operation": seen[1], "event_at": seen[2],
                "created_at": row["created_at"],
                "detail": (f"revision 是 {row['revision']}，而事件里最大只到 {seen[0]}"
                           f"（{seen[1]} @ {seen[2]}）：多出来的那一次递增没有对应操作")})
    return findings


def artifact_key_shape(conn, project_id=None):
    """`submissions.artifacts` 里键集合不是三键的项；`role` 那种行就落在这里。"""
    rows = conn.execute(
        "SELECT s.id, a.ticket_id, s.artifacts FROM submissions s JOIN attempts a ON a.id=s.attempt_id"
        + (" JOIN tickets t ON t.id=a.ticket_id WHERE t.project_id=?" if project_id else "")
        + " ORDER BY s.created_at, s.rowid",
        (project_id,) if project_id else ()).fetchall()
    findings = []
    for row in rows:
        try:
            items = json.loads(row["artifacts"])
        except (TypeError, ValueError):
            findings.append({
                "check": ARTIFACT_KEY_SHAPE, "ticket_id": row["ticket_id"],
                "submission_id": row["id"], "artifacts_index": None,
                "detail": "artifacts 不是一段能读的 JSON 数组"})
            continue
        if not isinstance(items, list):
            findings.append({
                "check": ARTIFACT_KEY_SHAPE, "ticket_id": row["ticket_id"],
                "submission_id": row["id"], "artifacts_index": None,
                "detail": f"artifacts 是 {type(items).__name__}，不是数组"})
            continue
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                findings.append({
                    "check": ARTIFACT_KEY_SHAPE, "ticket_id": row["ticket_id"],
                    "submission_id": row["id"], "artifacts_index": index,
                    "detail": f"这一项是 {type(item).__name__}，不是含三键的对象"})
                continue
            extra = [key for key in item if key not in ARTIFACT_KEYS]
            missing = [key for key in ARTIFACT_KEYS if key not in item]
            if not extra and not missing:
                continue
            findings.append({
                "check": ARTIFACT_KEY_SHAPE, "ticket_id": row["ticket_id"],
                "submission_id": row["id"], "artifacts_index": index,
                "extra_keys": extra, "missing_keys": missing,
                "detail": (f"artifacts[{index}] 的键是 {sorted(item)}，只允许 "
                           f"{' / '.join(ARTIFACT_KEYS)}；"
                           + (f"多出 {', '.join(extra)}" if extra else "")
                           + (f"缺少 {', '.join(missing)}" if missing else "")
                           + "——验收时按整份字典比对，这一项永远对不上")})
    return findings


def check(conn, project_id=None):
    """跑完三类，返回 `{"ok", "findings", "counts"}`。`ok` 只有一条判据：findings 为空。"""
    findings = (attempts_without_claim(conn, project_id)
                + revisions_without_event(conn, project_id)
                + artifact_key_shape(conn, project_id))
    counts = {name: sum(1 for item in findings if item["check"] == name) for name in CHECKS}
    return {"ok": not findings, "findings": findings, "counts": counts,
            "checks": [{"check": name, "meaning": MEANING[name], "suspicious": counts[name]}
                       for name in CHECKS]}
