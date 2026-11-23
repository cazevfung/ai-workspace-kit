#!/usr/bin/env python3
"""工单进度视图：把两个库算成「每张在途工单一张卡」（PRD §4.11）。

0.2 的项目级六态被用户否掉：「这个状态机没意义……我要的是工单，那个项目做到哪了，在干啥，
到什么进度，整个完成了还是在哪一个 step，完成率多少，有没有卡住」。0.3 起主视图按工单，
项目只当标签。

每张卡五步「派发 → 执行 → 提交 → 验收 → 合并」，阶段全部从库里算：

    派发  attempts 有行、state 还是 dispatching（或还没有人领）
    执行  attempt.state='running'，或已 submitted 但工单仍 running
    提交  最新 submissions 有、工单 awaiting_review、reviews 还没有行
    验收  reviews 有行（任何 verdict）；reject 回 rework 时当前步退回「执行」并打返工标记
    合并  merges 有该工单的行

百分比只有一个：已完成步数 / 5，分母固定，不随项目变（PRD §4.11）。

每次调用都重新读两个库，不缓存、不做增量（PRD §4.10）。
只读：取数全部走 tools/datalayer.py，两个库都以 mode=ro 打开并执行 PRAGMA query_only=ON。

0.4 起还有三件事（PRD §4.12–§4.14）：

    没挂工单的会话  已经挂在某张工单上的执行会话不重复列；顶栏在跑数按会话去重
    错位标注        执行会话标题里的单号与工单号不一致时在卡片上标一行
    状态可疑        活跃轮次字段非空、但不过 6 小时 / 进程号两道门槛的，不当成在跑

0.6 起会话区合成一块（PRD §4.15）：等你的（S4）与在跑的在同一个网格里，等你的排前面；
不再在页脚单列一段「像卡住了」，同一个会话只出现一次。阻断原因、错位警告与最后一句
在页面上最多两行（PRD §4.16）。
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import datalayer  # noqa: E402

# --------------------------------------------------------------- 五步与排序

# PRD §4.11 的五步，顺序写死；分母永远是 5
STEP_LABELS = ("派发", "执行", "提交", "验收", "合并")
STEP_KEYS = ("dispatch", "execute", "submit", "review", "merge")
STEPS_TOTAL = 5

# PRD §4.11 的排序：等你说 > 待验收 > 返工中 > 在跑 > 排队 > 已验收未合
BUCKET_WAITING = "waiting"
BUCKET_REVIEW = "review"
BUCKET_REWORK = "rework"
BUCKET_RUNNING = "running"
BUCKET_QUEUED = "queued"
BUCKET_UNMERGED = "unmerged"
BUCKET_ORDER = (
    BUCKET_WAITING,
    BUCKET_REVIEW,
    BUCKET_REWORK,
    BUCKET_RUNNING,
    BUCKET_QUEUED,
    BUCKET_UNMERGED,
)
BUCKET_LABELS = {
    BUCKET_WAITING: "等你说",
    BUCKET_REVIEW: "待验收",
    BUCKET_REWORK: "返工中",
    BUCKET_RUNNING: "在跑",
    BUCKET_QUEUED: "排队",
    BUCKET_UNMERGED: "已验收未合",
}
BUCKET_TONES = {
    BUCKET_WAITING: "orange",
    BUCKET_REVIEW: "blue",
    BUCKET_REWORK: "purple",
    BUCKET_RUNNING: "green",
    BUCKET_QUEUED: "steel",
    BUCKET_UNMERGED: "teal",
}

# 还没有人领的状态：派发中是「派发」这一步在等，ready 是还没派
QUEUED_TICKET_STATES = ("ready", "dispatching")
# 执行阶段的尝试状态：已提交但工单状态还没翻，仍然算在这一步
IN_PROGRESS_ATTEMPT_STATES = ("submitted", "accepted")
# 一行里标题的截断长度（页面还有 CSS 省略号兜底）
TITLE_LIMIT = 80
SESSION_TITLE_LIMIT = 60
# PRD §4.11 第三行：执行会话「最后一句前 60 字」
PREVIEW_LIMIT = 60
# 已完成折叠区最多列多少行：121 张已完成全铺开会把主视图挤没
DONE_ROW_LIMIT = 30
# 「状态可疑」列表的上限：同一台机器上大片没清标记的旧会话会把页面顶满
SUSPECT_ROW_LIMIT = 12
# PRD §4.13：会话标题里的工单号（例：CUT-001、DASH-005）
TICKET_IN_TITLE = re.compile(r"[A-Z]+-\d+")
# 会话卡的三种状态（PRD §4.15）：等你的（S4）与在跑的同一块，可疑的另列一块
SESSION_STATE_RUNNING = "running"
SESSION_STATE_WAITING = "waiting"
SESSION_STATE_SUSPECT = "suspect"
SESSION_WAITING_LABEL = "等你说"
SESSION_RUNNING_LABEL = "在跑"
SESSION_STATE_LABELS = {
    SESSION_STATE_RUNNING: SESSION_RUNNING_LABEL,
    SESSION_STATE_WAITING: SESSION_WAITING_LABEL,
    SESSION_STATE_SUSPECT: "状态可疑",
}
# 会话在工作里的角色：验收类与协调类也要列在会话区，标出来（PRD §4.12）
KIND_COORDINATOR = "coordinator"
KIND_REVIEW = "review"
KIND_SESSION = "session"
KIND_LABELS = {
    KIND_COORDINATOR: "协调",
    KIND_REVIEW: "验收",
    KIND_SESSION: SESSION_RUNNING_LABEL,
}


# --------------------------------------------------------------------------- 小工具


def _iso(seconds):
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds).astimezone().isoformat(timespec="seconds")


def _clock(seconds):
    if seconds is None:
        return ""
    return datetime.fromtimestamp(seconds).strftime("%H:%M:%S")


def _ago(seconds):
    """「3 小时前」里的那个数字部分；None 就写未知。"""
    if seconds is None:
        return "时间未知"
    total = int(max(0, seconds))
    if total < 60:
        return "刚刚"
    if total < 3600:
        return f"{total // 60} 分钟"
    if total < 86400:
        return f"{total // 3600} 小时"
    return f"{total // 86400} 天"


def _json_list(value):
    """把库里的 JSON 文本列读成列表；不是 JSON、不是列表就当空，不猜内容。"""
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _latest(rows, key="created_at"):
    best = None
    best_at = None
    for row in rows:
        stamp = datalayer.to_seconds(row.get(key))
        if stamp is None:
            continue
        if best is None or stamp > best_at:
            best, best_at = row, stamp
    return best, best_at


# --------------------------------------------------------------------------- 阶段判定


def steps_for(completed):
    """五步的逐格状态：已走完的打勾、当前步高亮、未到灰。"""
    steps = []
    for index, label in enumerate(STEP_LABELS):
        if completed >= STEPS_TOTAL or index < completed:
            state = "done"
        elif index == completed:
            state = "current"
        else:
            state = "pending"
        steps.append({"label": label, "state": state})
    return steps


def stage_completed(ticket, attempt, latest_submission, latest_review, merged):
    """当前走到第几步：返回「已完成的步数」，0–5。

    PRD §4.11 的阶段表逐条对着写；reject 之后工单回到 rework，当前步退回「执行」，
    所以返工中的单已完成步数是 1，不是 3。
    """
    state = ticket.get("state")
    verdict = (latest_review or {}).get("verdict")
    if merged:
        return STEPS_TOTAL
    if verdict == "reject" and state != "done":
        return 1
    if latest_review is not None:
        # reviews 有行就是「验收」这一步在做；工单已 done 说明验收过了，卡在合并
        return 4 if state == "done" else 3
    if state == "done":
        # 状态说 done、却没有 review 行：不猜，按已验收未合处理（是否真待合并另有判据）
        return 4
    if latest_submission is not None and state == "awaiting_review":
        return 2
    if attempt:
        attempt_state = attempt.get("state")
        if attempt_state == "dispatching":
            # 派发还没被接走：这一步正在做，已完成的步数是 0
            return 0
        if attempt_state == "running" or attempt_state in IN_PROGRESS_ATTEMPT_STATES:
            return 1
        # blocked / rejected / superseded 且还没提交：做过，但仍在执行这一步
        return 1
    return 0


def bucket_of(ticket, attempt, latest_review, has_open_question, completed):
    """排序用的六类。命中第一条就返回，顺序与 PRD §4.11 的排序一致。"""
    state = ticket.get("state")
    if state == "blocked" or has_open_question:
        return BUCKET_WAITING
    if state == "rework":
        return BUCKET_REWORK
    if latest_review is not None and latest_review.get("verdict") == "reject" and state != "done":
        return BUCKET_REWORK
    if state == "awaiting_review" and latest_review is None and completed == 2:
        return BUCKET_REVIEW
    if attempt is not None and attempt.get("state") == "running":
        return BUCKET_RUNNING
    if state == "awaiting_review":
        # reviews 已经有行、工单状态还没翻：仍在验收这一步等人看
        return BUCKET_REVIEW
    if state in QUEUED_TICKET_STATES:
        return BUCKET_QUEUED
    if state == "running":
        # 已提交但工单状态还没翻：仍在执行阶段
        return BUCKET_RUNNING
    if state == "done":
        return BUCKET_UNMERGED
    return BUCKET_QUEUED


# --------------------------------------------------------------------------- 三处进度文字


def criteria_progress(ticket, latest_review):
    """PRD §4.11 第三行：没 review 写「10 条待验收」，reject 写「上轮 9 过 1 不过」+ 失败编号，accept 写「10/10 通过」。

    条数来自两处：`tickets.criteria` 是这张单的验收条件清单，`reviews.checks` 是验收人逐条打的结果。
    有 review 时以 checks 为准（那才是被打过的条目），没有 review 时只能数 criteria。
    """
    criteria = _json_list(ticket.get("criteria"))
    total_criteria = len(criteria)
    if latest_review is None:
        if total_criteria:
            return {
                "text": f"{total_criteria} 条待验收",
                "total": total_criteria,
                "passed": None,
                "failed": [],
            }
        return {"text": "验收条件数未知", "total": None, "passed": None, "failed": []}
    checks = _json_list(latest_review.get("checks"))
    passed = [item for item in checks if item.get("passed")]
    failed = [item.get("id") or "?" for item in checks if not item.get("passed")]
    total = max(len(checks), total_criteria)
    if latest_review.get("verdict") == "reject":
        text = f"上轮 {len(passed)} 过 {len(failed)} 不过"
        if failed:
            text += " · 不过：" + "、".join(str(item) for item in failed)
    else:
        text = f"{len(passed)}/{total} 通过"
        if failed:
            text += " · 不过：" + "、".join(str(item) for item in failed)
    return {"text": text, "total": total, "passed": len(passed), "failed": failed}


def side_note(bucket, ticket, latest_review, question, elapsed_text, head_sha, tip, merge_reason, rework_count):
    """PRD §4.11 / §4.16 / §4.19：一句话与它的全文。

    原因不在这里截断：页面上限两行是 CSS 的事，`title` 必须是全文。
    DASH-006 r1 的缺陷就是这里先截到 60 字再拼进 title，200 字的原因悬停也只能看到 77 字。
    """
    if bucket == BUCKET_WAITING:
        reason = ""
        if question is not None:
            reason = datalayer.collapse(question.get("question"))
        elif latest_review is not None:
            reason = datalayer.collapse(latest_review.get("reason"))
        if not reason and ticket.get("state") == "blocked":
            reason = "未记录原因"
        if reason:
            text = f"受阻：{reason} · 等了 {elapsed_text}"
            return {"kind": "blocked", "text": text, "title": text}
        return None
    if bucket == BUCKET_REWORK:
        text = f"返工 {rework_count} 次"
        return {"kind": "rework", "text": text, "title": text}
    if bucket == BUCKET_UNMERGED:
        if head_sha and tip:
            text = f"待合并：head {head_sha[:8]} · 分支 tip {tip[:8]}"
            return {
                "kind": "merge",
                "text": text,
                "title": text,
                "head_sha8": head_sha[:8],
                "tip_sha8": tip[:8],
            }
        text = f"待合并：{merge_reason or '判据不全'}" if merge_reason else "待合并"
        return {"kind": "merge", "text": text, "title": text}
    return None


# --------------------------------------------------------------------------- 卡片


def _session_view(session, attempt, now):
    """第四行：执行会话的标题、最后一句前 60 字、已跑多久。"""
    elapsed = None
    running = False
    if session is not None and datalayer.session_live(session, now):
        running = True
        started = datalayer.to_seconds(session.get("active_turn_started_at"))
        if started is not None:
            elapsed = max(0.0, now - started)
    if elapsed is None:
        started = datalayer.to_seconds((attempt or {}).get("created_at"))
        if started is not None:
            elapsed = max(0.0, now - started)
    return {
        "id": (session or {}).get("id") or (attempt or {}).get("session_id"),
        "title": datalayer.collapse((session or {}).get("title"), SESSION_TITLE_LIMIT) or "未记录会话",
        "preview": datalayer.collapse((session or {}).get("list_preview"), PREVIEW_LIMIT),
        "running": running,
        "elapsed_seconds": elapsed,
        "elapsed_text": datalayer.human_seconds(elapsed),
    }


def ticket_in_title(title):
    """PRD §4.13：从会话标题里取单号；取不到就返回 None，不猜。"""
    match = TICKET_IN_TITLE.search(title or "")
    return match.group(0) if match else None


def in_flight_kind(session, now):
    """会话当下算「在跑」、「状态可疑」还是两者都不是。"""
    if session is None:
        return None
    if datalayer.session_live(session, now):
        return "running"
    if datalayer.session_suspect(session, now):
        return "suspect"
    return None


def session_mismatch(ticket, session, kind):
    """PRD §4.13：执行会话标题里的单号与工单号不一致时标一行，不假装这张单在执行。

    标题里找不到单号就不标——那是猜。会话不是「在跑 / 状态可疑」时也不标：
    把一张没人动的旧会话写成「会话在跑 X」，比不标更糟。
    """
    if session is None or kind is None:
        return None
    other = ticket_in_title(session.get("title"))
    if not other or other == ticket.get("id"):
        return None
    if kind == "running":
        text = f"会话在跑 {other}"
    else:
        text = f"会话状态可疑，标题写着 {other}"
    return {
        "ticket_id": other,
        "session_id": session.get("id"),
        "session_title": datalayer.collapse(session.get("title"), SESSION_TITLE_LIMIT),
        "session_running": kind == "running",
        "text": text,
    }


def _session_role(session, coordinator_ids):
    """PRD §4.12：协调 / 验收 / 普通会话三种角色，只看库里已有的字段，不猜。"""
    if session.get("id") in coordinator_ids:
        return KIND_COORDINATOR
    if "验收" in str(session.get("title") or ""):
        return KIND_REVIEW
    return KIND_SESSION


def _dir_tail(path):
    """PRD §4.21：会话卡第二行只留项目目录的最后一段。"""
    parts = [piece for piece in re.split(r"[\\/]+", str(path or "")) if piece]
    return parts[-1] if parts else ""


def _session_card(session, now, kind_label, role, running, state=None, since=None):
    """PRD §4.12 / §4.15 / §4.21：会话区一张小卡：两行——状态点+标题、目录最后一段+等了/跑了多久。

    `state` 决定卡片状态标与动效（在跑 / 等你说 / 状态可疑）；`since` 是计时的起点：
    在跑与可疑从活跃轮次开始算，等你的（S4）从最后一句活动算（那才是「等了多久」）。
    """
    if since is None:
        since = datalayer.to_seconds(session.get("active_turn_started_at"))
    elapsed = None if since is None else max(0.0, now - since)
    role_label = KIND_LABELS.get(role)
    state_value = state or (SESSION_STATE_RUNNING if running else SESSION_STATE_SUSPECT)
    return {
        "id": session.get("id"),
        "session_id": session.get("id"),
        "title": datalayer.collapse(session.get("title"), SESSION_TITLE_LIMIT) or "（无标题）",
        "working_dir": datalayer.collapse(session.get("working_dir"), 120),
        "dir_tail": _dir_tail(session.get("working_dir")),
        "preview": datalayer.collapse(session.get("list_preview"), PREVIEW_LIMIT),
        "kind": role,
        "kind_label": kind_label,
        # 状态标与角色标分开：等你说 / 状态可疑 时，协调/验收这枚角色标还要能看见
        "role_label": role_label if role in (KIND_COORDINATOR, KIND_REVIEW) else None,
        "state": state_value,
        # 会话区同时列着在跑与等你的，每张卡自己写清是哪一种（页面第二行行首）
        "state_label": SESSION_STATE_LABELS[state_value],
        "running": bool(running),
        "since": _iso(since),
        "since_epoch": since,
        "elapsed_seconds": elapsed,
        "elapsed_text": datalayer.human_seconds(elapsed),
    }


def suspect_reason(session, now):
    """PRD §4.2「状态可疑」的原因（0.5 只剩一条）：已超过 6 小时。"""
    started = datalayer.to_seconds(session.get("active_turn_started_at"))
    if started is None:
        return "无活跃轮次时间"
    hours = int((now - started) // 3600)
    return f"已超过 6 小时（{hours} 小时前开始）"


def card_sentence(bucket, checks, session, note, elapsed_text):
    """PRD §4.19 第三层：按状态只给一句话，其余可空。

    阻断原因与待合并那类直接用 side_note 的全文；返工优先写不过的编号——返工次数
    已经变成状态点上的小圆标（§4.20），不再另起一行。
    """
    if note and note.get("text"):
        if bucket == BUCKET_REWORK:
            failed = (checks or {}).get("failed") or []
            if failed:
                return "不过：" + "、".join(str(item) for item in failed)
        return note["text"]
    if bucket == BUCKET_RUNNING:
        elapsed = (session or {}).get("elapsed_text")
        return f"已跑 {elapsed}" if elapsed else ""
    if bucket == BUCKET_REVIEW:
        total = (checks or {}).get("total")
        if total:
            return f"{total} 条待验收 · 已等 {elapsed_text}"
        return f"已等 {elapsed_text}"
    return ""


def _card(ticket, project, bucket, completed, since, now, attempt, session, checks, note, running=False,
          mismatch=None):
    elapsed = None if since is None else max(0.0, now - since)
    elapsed_text = datalayer.human_seconds(elapsed)
    percent = int(round(completed * 100.0 / STEPS_TOTAL))
    stage_index = min(completed, STEPS_TOTAL - 1)
    session_view = _session_view(session, attempt, now)
    return {
        "id": ticket["id"],
        "ticket_id": ticket["id"],
        "project_id": project.get("id") if project else ticket.get("project_id"),
        "project": (project or {}).get("name") or ticket.get("project_id") or "",
        "title": datalayer.collapse(ticket.get("title"), TITLE_LIMIT),
        "bucket": bucket,
        "bucket_label": BUCKET_LABELS[bucket],
        "tone": BUCKET_TONES[bucket],
        "steps": steps_for(completed),
        # stage 是当前这一步（PRD §4.11 的五个名字之一），stage_key 给机器对
        "stage": STEP_LABELS[stage_index],
        "stage_key": STEP_KEYS[stage_index],
        "step_index": stage_index,
        "steps_done": completed,
        "steps_total": STEPS_TOTAL,
        "percent": percent,
        "progress_text": f"{completed}/{STEPS_TOTAL} · {percent}%",
        "checks": checks,
        "session": session_view,
        # PRD §4.19：卡片第三层的一句话；全文进 title（原因不在这里截断）
        "sentence": card_sentence(bucket, checks, session_view, note, elapsed_text),
        # PRD §4.13：执行会话标题里的单号与工单号不一致时标一行，带那个会话的链接
        "session_mismatch": mismatch,
        "note": note,
        "rework_count": 0,
        "running": running,
        "watch_session": (attempt or {}).get("session_id"),
        "pm_session": (attempt or {}).get("coordinator_session_id"),
        "since": _iso(since),
        "since_epoch": since,
        "elapsed_seconds": elapsed,
        "elapsed_text": elapsed_text,
    }


def _done_row(ticket, project, kind, at, now, review, sha, reason):
    checks = _json_list((review or {}).get("checks"))
    passed = len([item for item in checks if item.get("passed")])
    total = max(len(checks), len(_json_list(ticket.get("criteria"))))
    ago = _ago(None if at is None else max(0.0, now - at))
    score = f"{passed}/{total} 通过" if total else "已验收"
    if kind == "merged":
        text = f"{ticket['id']} ✓ {score} · 合入 {(sha or '')[:8]} · {ago}前"
    else:
        text = f"{ticket['id']} ✓ {score} · 历史直做（{reason or '无合并记录'}） · {ago}前"
    return {
        "id": ticket["id"],
        "ticket_id": ticket["id"],
        "project_id": (project or {}).get("id") or ticket.get("project_id"),
        "project": (project or {}).get("name") or "",
        "title": datalayer.collapse(ticket.get("title"), TITLE_LIMIT),
        "kind": kind,
        "text": text,
        "at": _iso(at),
        "at_epoch": at,
        "ago_text": ago,
        "merged_sha": (sha or "")[:8] or None,
    }


# --------------------------------------------------------------------------- 主函数


def build_overview(center, cindy, now, branch_tips=None, repo_check=None):
    """把两个库的原始行算成工单进度视图。纯函数：不连库、不写盘，测试直接塞行进来。

    branch_tips(root) 返回该仓库 refs/heads/ticket/* 的 {工单号: tip}；每个仓库每轮只问一次 git。
    """
    branch_tips = datalayer.git_branch_tips if branch_tips is None else branch_tips
    repo_check = datalayer.is_git_repo if repo_check is None else repo_check

    issues = []
    if not center.get("ok"):
        issues.append(f"中心库读不到：{center.get('path')}｜{center.get('error')}")
    if not cindy.get("ok"):
        issues.append(f"Cindy 主库读不到：{cindy.get('path') or '未找到'}｜{cindy.get('error')}")

    center_rows = center.get("rows") or {}
    projects = center_rows.get("projects", [])
    tickets = center_rows.get("tickets", [])
    attempts = center_rows.get("attempts", [])
    submissions = center_rows.get("submissions", [])
    reviews = center_rows.get("reviews", [])
    merges = center_rows.get("merges", [])
    questions = center_rows.get("questions", [])
    sessions = (cindy.get("rows") or {}).get("sessions", [])

    project_by_id = {row["id"]: row for row in projects}
    attempts_by_id = {row["id"]: row for row in attempts}
    attempts_by_ticket = defaultdict(list)
    for row in attempts:
        attempts_by_ticket[row["ticket_id"]].append(row)
    submissions_by_attempt = defaultdict(list)
    for row in submissions:
        submissions_by_attempt[row["attempt_id"]].append(row)
    review_by_submission = {row["submission_id"]: row for row in reviews}
    merge_by_ticket = {row["ticket_id"]: row for row in merges}
    sessions_by_id = {row["id"]: row for row in sessions}
    open_questions = defaultdict(list)
    for row in questions:
        if row.get("state") == "open":
            open_questions[row["ticket_id"]].append(row)

    tips_by_root = {}

    def tips_for(root):
        """每个仓库每轮只问一次 git；不是仓库返回 None，与「无仓库」区分。"""
        if root not in tips_by_root:
            tips_by_root[root] = branch_tips(root) if repo_check(root) else None
        return tips_by_root[root]

    def latest_submission_of(ticket_id):
        """该工单全部尝试里 created_at 最大的一条提交（返工后旧提交还留着，不能只看 current_attempt）。"""
        return _latest(
            [sub for attempt in attempts_by_ticket.get(ticket_id, []) for sub in submissions_by_attempt.get(attempt["id"], [])]
        )

    def reviews_of(ticket_id):
        rows = []
        for attempt in attempts_by_ticket.get(ticket_id, []):
            for sub in submissions_by_attempt.get(attempt["id"], []):
                review = review_by_submission.get(sub["id"])
                if review is not None:
                    rows.append(review)
        return rows

    def latest_review_of(ticket_id):
        return _latest(reviews_of(ticket_id))

    def in_flight_session_of(ticket_id):
        """这张单名下（含已 superseded 的尝试）当下最可能还活着的那个会话。

        PRD §4.13 的实测形状是：CUT-003 当前尝试还是 dispatching、还没绑会话，
        真正在跑的是前几次尝试用过的那个会话（标题写着 CUT-001）。只看 current_attempt
        就会把这一单当成没人动，也就看见错位。
        """
        best = None
        best_at = 0.0
        for attempt in attempts_by_ticket.get(ticket_id, []):
            session = sessions_by_id.get(attempt.get("session_id") or "")
            if in_flight_kind(session, now) is None:
                continue
            started = datalayer.to_seconds(session.get("active_turn_started_at")) or 0.0
            if best is None or started >= best_at:
                best, best_at = session, started
        return best

    cards = []
    done_rows = []
    done_today = 0
    today = datetime.fromtimestamp(now).date()

    for ticket in tickets:
        state = ticket.get("state")
        if state == "cancelled":
            continue
        project = project_by_id.get(ticket.get("project_id"))
        attempt = attempts_by_id.get(ticket.get("current_attempt") or "")
        submission, _ = latest_submission_of(ticket["id"])
        review, review_at = latest_review_of(ticket["id"])
        merge = merge_by_ticket.get(ticket["id"])
        merged = merge is not None
        session = sessions_by_id.get((attempt or {}).get("session_id") or "")
        completed = stage_completed(ticket, attempt, submission, review, merged)
        attempt_running = (attempt or {}).get("state") == "running"
        rework_count = len([row for row in reviews_of(ticket["id"]) if row.get("verdict") == "reject"])
        runner = in_flight_session_of(ticket["id"])
        mismatch = session_mismatch(ticket, runner, in_flight_kind(runner, now))

        if state == "done":
            if merged:
                done_today += 1 if _same_day(merge.get("created_at"), today) else 0
                done_rows.append(
                    _done_row(ticket, project, "merged", datalayer.to_seconds(merge.get("created_at")), now,
                              review, merge.get("commit_sha"), None)
                )
                continue
            head_sha = (submission or {}).get("head_sha") or ""
            root = (project or {}).get("root") or ""
            tips = tips_for(root)
            kind, reason = datalayer.classify_merge(head_sha, tips is not None, (tips or {}).get(ticket["id"]))
            if kind != "pending":
                done_rows.append(
                    _done_row(ticket, project, "history", review_at or datalayer.to_seconds(ticket.get("created_at")),
                              now, review, None, reason)
                )
                continue
            bucket = BUCKET_UNMERGED
            since = review_at or datalayer.to_seconds((submission or {}).get("created_at"))
            criteria = criteria_progress(ticket, review)
            note = side_note(bucket, ticket, review, None,
                             datalayer.human_seconds(None if since is None else max(0.0, now - since)),
                             head_sha, tips.get(ticket["id"]), reason, 0)
            cards.append(
                _card(ticket, project, bucket, completed, since, now, attempt, session, criteria, note,
                      attempt_running, mismatch)
            )
            continue

        waiting_questions = open_questions.get(ticket["id"], [])
        question = max(waiting_questions, key=lambda row: datalayer.to_seconds(row.get("asked_at")) or 0) \
            if waiting_questions else None
        bucket = bucket_of(ticket, attempt, review, bool(waiting_questions), completed)

        if bucket == BUCKET_WAITING:
            since = datalayer.to_seconds((question or {}).get("asked_at")) or datalayer.to_seconds(ticket.get("created_at"))
        elif bucket == BUCKET_REVIEW:
            since = datalayer.to_seconds((submission or {}).get("created_at")) or datalayer.to_seconds(ticket.get("created_at"))
        elif bucket == BUCKET_REWORK:
            since = review_at or datalayer.to_seconds(ticket.get("created_at"))
        elif bucket == BUCKET_RUNNING:
            since = datalayer.to_seconds((attempt or {}).get("created_at"))
            if since is None and session is not None:
                since = datalayer.to_seconds(session.get("active_turn_started_at"))
        else:
            since = datalayer.to_seconds(ticket.get("created_at"))

        elapsed_text = datalayer.human_seconds(None if since is None else max(0.0, now - since))
        card = _card(
            ticket, project, bucket, completed, since, now, attempt, session,
            criteria_progress(ticket, review),
            side_note(bucket, ticket, review, question, elapsed_text, None, None, None, rework_count),
            attempt_running, mismatch,
        )
        card["rework_count"] = rework_count
        cards.append(card)

    cards.sort(
        key=lambda card: (
            BUCKET_ORDER.index(card["bucket"]),
            -(card["elapsed_seconds"] or 0),
            card["id"],
        )
    )
    done_rows.sort(key=lambda row: (-(row["at_epoch"] or 0), row["id"]))

    # ---- PRD §4.12 / §4.15：会话区只有一块。没挂工单的会话各一张卡，按状态分两堆：
    # 等你的（S4）排在前面、橙边框 4 秒脉冲；在跑的在后面。同一会话只出现一次——
    # 已经挂在某张工单上的会话不进会话区，工单卡自己已经展示了它。
    # 页面不再单列一段「像卡住了」：那会把同一个会话画两遍。
    attached_ids = {row.get("session_id") for row in attempts if row.get("session_id")}
    coordinator_ids = {row.get("coordinator_session_id") for row in attempts if row.get("coordinator_session_id")}
    orphan_sessions = []
    suspect_sessions = []
    for session in sessions:
        sid = session.get("id")
        # 只跳过挂在工单上当作执行会话的那一个；协调会话不进工单卡的主位，仍要在会话区看得见
        if sid in attached_ids:
            continue
        if datalayer.session_stuck(session, now):
            since = datalayer.to_seconds(session.get("last_activity_at"))
            row = _session_card(session, now, SESSION_WAITING_LABEL,
                                _session_role(session, coordinator_ids), False,
                                state=SESSION_STATE_WAITING, since=since)
            row["reason"] = "推断：最后一句是你说的，超过 10 分钟没人接"
            orphan_sessions.append(row)
            continue
        if not datalayer.session_active(session):
            continue
        kind = in_flight_kind(session, now)
        if kind is None:
            continue
        role = _session_role(session, coordinator_ids)
        if kind == "running":
            orphan_sessions.append(_session_card(session, now, KIND_LABELS[role], role, True,
                                                 state=SESSION_STATE_RUNNING))
            continue
        row = _session_card(session, now, "状态可疑", role, False, state=SESSION_STATE_SUSPECT)
        row["reason"] = suspect_reason(session, now)
        suspect_sessions.append(row)

    # 等你的排前面；同一堆里等/跑得久的在前。
    orphan_sessions.sort(key=lambda row: (0 if row["state"] == SESSION_STATE_WAITING else 1,
                                          -(row["since_epoch"] or 0), row["id"]))
    suspect_sessions.sort(key=lambda row: -(row["since_epoch"] or 0))

    # PRD §4.12：顶栏的在跑数按会话去重算，不只数工单。
    # 工单侧的 running 尝试也是「一件事在动」：它的执行会话可能不在这个库里、
    # 两个来源取并集，同一会话只算一次；没有会话号的尝试用它自己的工单号当键。
    # 等你的会话不算在跑（它没在动）。
    live_keys = set()
    for card in cards:
        if card["running"]:
            live_keys.add(card.get("watch_session") or "ticket:" + card["id"])
    live_keys |= {row["id"] for row in orphan_sessions if row["state"] == SESSION_STATE_RUNNING}

    # PRD §4.17（0.7）：顶栏「等你」跨两块算——工单侧 open question / blocked 的工单，
    # 加 S4 命中的会话。同一对象只算一次：挂在这些工单上的会话本来就不进会话区，
    # 两边用 ticket: / session: 分开做键，日后放开也会先在这里去重。
    waiting_keys = {f"ticket:{card['ticket_id']}" for card in cards if card["bucket"] == BUCKET_WAITING}
    waiting_keys |= {f"session:{row['id']}" for row in orphan_sessions
                     if row["state"] == SESSION_STATE_WAITING}

    summary = {
        "inflight": len(cards),
        "running": len(live_keys),
        "waiting": len(waiting_keys),
        "review": len([card for card in cards if card["bucket"] == BUCKET_REVIEW]),
        "done_today": done_today,
        "orphan": len(orphan_sessions),
        "suspect": len(suspect_sessions),
    }

    return {
        "generated_at": _iso(now),
        "generated_clock": _clock(now),
        "generated_epoch_ms": int(now * 1000),
        "sources": {
            "center": {
                "path": center.get("path"),
                "ok": bool(center.get("ok")),
                "error": center.get("error"),
                "schema": center.get("schema"),
                "read_at": center.get("read_at"),
                "read_clock": _clock(center.get("read_at")),
            },
            "cindy": {
                "path": cindy.get("path"),
                "name": cindy.get("name"),
                "mtime": cindy.get("mtime"),
                "mtime_text": cindy.get("mtime_text") or "无",
                "ok": bool(cindy.get("ok")),
                "error": cindy.get("error"),
                "read_at": cindy.get("read_at"),
                "read_clock": _clock(cindy.get("read_at")),
            },
        },
        "issues": issues,
        "summary": summary,
        "tickets": cards,
        "orphan_sessions": orphan_sessions,
        "suspect_sessions": suspect_sessions[:SUSPECT_ROW_LIMIT],
        "suspect_total": len(suspect_sessions),
        "done": done_rows[:DONE_ROW_LIMIT],
        "done_total": len(done_rows),
    }


def _same_day(value, day):
    """中心库时间列 → 本地日期，和 now 的本地日期比。"""
    stamp = datalayer.to_seconds(value)
    if stamp is None:
        return False
    return datetime.fromtimestamp(stamp).date() == day


def collect_overview(center_path=datalayer.CENTER_DB, cindy_dir=None, now=None):
    """读一次两个库并算出 overview。服务每次请求都调用它：没有缓存。"""
    loaded = datalayer.load_databases(center_path=center_path, cindy_dir=cindy_dir, now=now)
    return build_overview(loaded["center"], loaded["cindy"], loaded["now"])


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    print(json.dumps(collect_overview(), ensure_ascii=False, indent=2))
