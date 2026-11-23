#!/usr/bin/env python3
"""生成 dashboard.html：只读两个库，把项目进度渲染成一个自包含的静态页。

这是第一段（DASH-001 / DASH-002）的产物，保留着当回归对照与离线查看用。
0.2 起主入口换成 tools/serve.py 的实时状态机页面；这个脚本不再扩写视图口径。

用法（零参数，在仓库根执行）：

    python tools/snapshot.py

取数与判据都在 tools/datalayer.py，只读：mode=ro 打开并执行 PRAGMA query_only=ON，
代码里只有 SELECT。
"""

from __future__ import annotations

import html
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from datalayer import (  # noqa: E402
    ACTIVE_TICKET_STATES,
    CENTER_DB,
    COPY_TEMPLATE,
    PREVIEW_LIMIT,
    RECENT_DAYS,
    REVIEW_TIMEOUT_S,
    classify_merge,
    clock_text,
    collapse,
    connect_readonly,
    day_text,
    find_cindy_db,
    git_branch_tip,
    human_seconds,
    is_git_repo,
    load_databases,
    read_round,
    session_active,
    session_idle,
    session_running,
    session_stuck,
    to_seconds,
)

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "dashboard.html"

def build_snapshot(center, cindy, now, git_tip=git_branch_tip, repo_check=is_git_repo, round_reader=read_round):
    """把两个库的原始行算成页面要的数字。纯函数：不连库、不写盘，测试直接塞行进来。"""
    issues = []
    if not center["ok"]:
        issues.append(f"中心库读不到：{center['path']}｜{center['error']}")
    if not cindy["ok"]:
        issues.append(f"Cindy 主库读不到：{cindy['path'] or '未找到'}｜{cindy['error']}")

    center_rows = center.get("rows") or {}
    tickets = center_rows.get("tickets", [])
    attempts = center_rows.get("attempts", [])
    submissions = center_rows.get("submissions", [])
    reviews = center_rows.get("reviews", [])
    merges = center_rows.get("merges", [])
    questions = center_rows.get("questions", [])
    projects = center_rows.get("projects", [])
    sessions = (cindy.get("rows") or {}).get("sessions", [])

    attempts_by_id = {row["id"]: row for row in attempts}
    attempts_by_ticket = defaultdict(list)
    for row in attempts:
        attempts_by_ticket[row["ticket_id"]].append(row)
    submissions_by_attempt = defaultdict(list)
    for row in submissions:
        submissions_by_attempt[row["attempt_id"]].append(row)
    submissions_by_id = {row["id"]: row for row in submissions}
    review_by_submission = {row["submission_id"]: row for row in reviews}
    merge_by_ticket = {row["ticket_id"]: row for row in merges}
    sessions_by_id = {row["id"]: row for row in sessions}
    tickets_by_id = {row["id"]: row for row in tickets}
    project_by_id = {row["id"]: row for row in projects}

    def latest_submission(ticket_id):
        best = None
        best_at = None
        for attempt in attempts_by_ticket.get(ticket_id, []):
            for submission in submissions_by_attempt.get(attempt["id"], []):
                stamp = to_seconds(submission["created_at"]) or 0
                if best is None or stamp > best_at:
                    best, best_at = submission, stamp
        return best

    recent_cutoff = now - RECENT_DAYS * 86400

    # ---- 正在跑：工单侧（current_attempt 指向的尝试 state='running'）
    running = []
    for ticket in tickets:
        if ticket["state"] not in ACTIVE_TICKET_STATES:
            continue
        attempt = attempts_by_id.get(ticket.get("current_attempt") or "")
        if not attempt or attempt["state"] != "running":
            continue
        started = to_seconds(attempt["created_at"])
        session = sessions_by_id.get(attempt.get("session_id") or "")
        running.append(
            {
                "ticket": ticket["id"],
                "title": ticket["title"],
                "project": ticket["project_id"],
                "project_name": (project_by_id.get(ticket["project_id"]) or {}).get("name") or ticket["project_id"],
                "attempt": attempt["id"],
                "elapsed": None if started is None else now - started,
                "session_id": attempt.get("session_id"),
                "coordinator_id": attempt.get("coordinator_session_id"),
                "session_title": (session or {}).get("title"),
                "session_head": collapse((session or {}).get("list_preview"), PREVIEW_LIMIT) or "（暂无预览）",
                "session_running": bool(session) and session_running(session, now),
            }
        )
    running.sort(key=lambda item: item["elapsed"] or 0, reverse=True)

    # ---- 会话侧：在跑但没有在途工单的会话，单独标一次（PRD 4.2 两个来源各标一次）
    ticket_session_ids = {item["session_id"] for item in running if item["session_id"]}
    sessions_only = []
    for session in sessions:
        # D4 未定前不显示归档：归档会话不能因为轮次时间戳看起来没收尾，就被标成「在跑」。
        if not session_active(session):
            continue
        if not session_running(session, now):
            continue
        if session["id"] in ticket_session_ids:
            continue
        started = to_seconds(session.get("active_turn_started_at"))
        sessions_only.append(
            {
                "id": session["id"],
                "title": session.get("title") or "（无标题）",
                "elapsed": None if started is None else now - started,
            }
        )
    sessions_only.sort(key=lambda item: item["elapsed"] or 0, reverse=True)

    # ---- 需要你出手
    open_questions = []
    for question in questions:
        if question["state"] != "open":
            continue
        asked = to_seconds(question["asked_at"])
        ticket = tickets_by_id.get(question["ticket_id"]) or {}
        open_questions.append(
            {
                "id": question["id"],
                "ticket": question["ticket_id"],
                "title": ticket.get("title") or "",
                "project": ticket.get("project_id") or "",
                "question": collapse(question["question"], 200),
                "waited": None if asked is None else now - asked,
                "session_id": (attempts_by_id.get(question.get("attempt_id") or "") or {}).get("session_id"),
            }
        )
    open_questions.sort(key=lambda item: item["waited"] or 0, reverse=True)

    blocked = []
    for ticket in tickets:
        if ticket["state"] != "blocked":
            continue
        attempt = attempts_by_id.get(ticket.get("current_attempt") or "") or {}
        blocked.append(
            {
                "ticket": ticket["id"],
                "title": ticket["title"],
                "project": ticket["project_id"],
                "session_id": attempt.get("session_id"),
                "coordinator_id": attempt.get("coordinator_session_id"),
            }
        )

    stale_review = []
    for ticket in tickets:
        if ticket["state"] != "awaiting_review":
            continue
        submission = latest_submission(ticket["id"])
        if submission is None:
            continue
        if submission["id"] in review_by_submission:
            continue
        created = to_seconds(submission["created_at"])
        waited = None if created is None else now - created
        if waited is None or waited <= REVIEW_TIMEOUT_S:
            continue
        attempt = attempts_by_id.get(ticket.get("current_attempt") or "") or {}
        stale_review.append(
            {
                "ticket": ticket["id"],
                "title": ticket["title"],
                "project": ticket["project_id"],
                "submission": submission["id"],
                "waited": waited,
                "session_id": attempt.get("session_id"),
                "coordinator_id": attempt.get("coordinator_session_id"),
            }
        )
    stale_review.sort(key=lambda item: item["waited"] or 0, reverse=True)

    stuck = []
    for session in sessions:
        if not session_stuck(session, now):
            continue
        last = to_seconds(session.get("last_activity_at"))
        stuck.append(
            {
                "id": session["id"],
                "title": session.get("title") or "（无标题）",
                "waited": now - last,
                "at": last,
                "session_head": collapse(session.get("list_preview"), PREVIEW_LIMIT),
            }
        )
    stuck.sort(key=lambda item: item["waited"] or 0, reverse=True)

    # ---- 已验收未合：先按 list_unmerged 的原料取，再按 4.4 分两类
    merge_pending = []
    history = defaultdict(list)
    for ticket in tickets:
        if ticket["state"] != "done":
            continue
        if ticket["id"] in merge_by_ticket:
            continue
        submission = latest_submission(ticket["id"])
        head_sha = (submission or {}).get("head_sha") or ""
        root = (project_by_id.get(ticket["project_id"]) or {}).get("root") or ""
        repo_ok = repo_check(root)
        tip = git_tip(root, ticket["id"]) if repo_ok else None
        kind, reason = classify_merge(head_sha, repo_ok, tip)
        entry = {
            "ticket": ticket["id"],
            "title": ticket["title"],
            "project": ticket["project_id"],
            "project_name": (project_by_id.get(ticket["project_id"]) or {}).get("name") or ticket["project_id"],
            "head_sha": head_sha,
            "tip": tip,
            "reason": reason,
        }
        if kind == "pending":
            merge_pending.append(entry)
        else:
            history[ticket["project_id"]].append(entry)
    merge_pending.sort(key=lambda item: item["ticket"], reverse=True)
    for items in history.values():
        items.sort(key=lambda item: item["ticket"], reverse=True)

    # ---- 验收与最近完成
    accepted_tickets = set()
    recent_by_ticket = {}
    for review in reviews:
        if review["verdict"] != "accept":
            continue
        submission = submissions_by_id.get(review["submission_id"])
        if submission is None:
            continue
        attempt = attempts_by_id.get(submission["attempt_id"]) or {}
        ticket_id = attempt.get("ticket_id")
        if not ticket_id:
            continue
        accepted_tickets.add(ticket_id)
        created = to_seconds(review["created_at"])
        if created is None or created < recent_cutoff:
            continue
        previous = recent_by_ticket.get(ticket_id)
        if previous is None or created > previous["at"]:
            recent_by_ticket[ticket_id] = {"at": created, "review": review, "ticket": ticket_id}

    recent_days = defaultdict(list)
    for ticket_id, info in recent_by_ticket.items():
        ticket = tickets_by_id.get(ticket_id) or {}
        merge = merge_by_ticket.get(ticket_id)
        recent_days[day_text(info["at"])].append(
            {
                "ticket": ticket_id,
                "title": ticket.get("title") or "",
                "project": ticket.get("project_id") or "",
                "project_name": (project_by_id.get(ticket.get("project_id")) or {}).get("name") or "",
                "at": info["at"],
                "commit": (merge or {}).get("commit_sha") or "",
            }
        )
    recent_groups = []
    for day, items in recent_days.items():
        items.sort(key=lambda item: item["at"], reverse=True)
        recent_groups.append({"day": day, "items": items, "stamp": items[0]["at"]})
    recent_groups.sort(key=lambda group: group["stamp"], reverse=True)

    # ---- 项目分组
    project_cards = []
    for project in projects:
        pid = project["id"]
        inflight = sum(1 for ticket in tickets if ticket["project_id"] == pid and ticket["state"] in ACTIVE_TICKET_STATES)
        accepted = sum(1 for ticket_id in accepted_tickets if (tickets_by_id.get(ticket_id) or {}).get("project_id") == pid)
        recent = sum(1 for ticket_id in recent_by_ticket if (tickets_by_id.get(ticket_id) or {}).get("project_id") == pid)
        project_cards.append(
            {
                "id": pid,
                "name": project["name"],
                "root": project["root"],
                "inflight": inflight,
                "accepted": accepted,
                "recent": recent,
                "history": history.get(pid, []),
                "round": round_reader(project["root"]),
            }
        )
    project_cards.sort(key=lambda card: (card["inflight"], card["recent"], card["accepted"], card["name"]), reverse=True)

    attention_counts = {
        "questions": len(open_questions),
        "blocked": len(blocked),
        "stale_review": len(stale_review),
        "stuck": len(stuck),
        "merge": len(merge_pending),
    }
    attention_total = sum(attention_counts.values())

    return {
        "now": now,
        "center": {
            "path": center["path"],
            "ok": center["ok"],
            "error": center["error"],
            "read_at": center["read_at"],
            "schema": center.get("schema"),
        },
        "cindy": {
            "path": cindy["path"],
            "name": cindy.get("name"),
            "mtime": cindy.get("mtime"),
            "mtime_text": cindy.get("mtime_text"),
            "ok": cindy["ok"],
            "error": cindy["error"],
            "read_at": cindy["read_at"],
        },
        "issues": issues,
        "stats": {
            "projects": len(projects),
            "running": len(running),
            "attention": attention_total,
            "recent": len(recent_by_ticket),
        },
        "attention": {
            "counts": attention_counts,
            "questions": open_questions,
            "blocked": blocked,
            "stale_review": stale_review,
            "stuck": stuck,
            "merge_pending": merge_pending,
        },
        "running": running,
        "sessions_only": sessions_only,
        "merge_pending": merge_pending,
        "history": {pid: items for pid, items in history.items()},
        "projects": project_cards,
        "recent": recent_groups,
    }


# --------------------------------------------------------------------------- 渲染


def esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def opener_for(ticket_id, title):
    return COPY_TEMPLATE.format(ticket=ticket_id, title=title)


def copy_button(text, label="复制开场白"):
    return f'<button class="btn" type="button" data-copy="{esc(text)}">{esc(label)}</button>'


def jump(session_id, label, opener, extra=""):
    if not session_id:
        return f'<span class="btn off">{esc(label)} · 未记录</span>'
    return (
        f'<a class="btn" href="cindy://session/{esc(session_id)}">{esc(label)}</a>'
        f"{copy_button(opener)}"
        f"{extra}"
    )


def ticket_actions(ticket_id, title, session_id, coordinator_id):
    opener = opener_for(ticket_id, title)
    same = ""
    if session_id and coordinator_id and session_id == coordinator_id:
        same = '<span class="tag">同一会话</span>'
    return (
        '<div class="row">'
        + jump(session_id, "看现场", opener)
        + jump(coordinator_id, "找 PM", opener)
        + same
        + "</div>"
    )


def render(snapshot):
    now = snapshot["now"]
    stats = snapshot["stats"]
    attention = snapshot["attention"]
    counts = attention["counts"]

    body = []
    body.append('<div class="wrap">')
    body.append("<h1>项目进度面板</h1>")
    body.append('<div class="stats">')
    for label, value in (
        ("项目", stats["projects"]),
        ("在跑", stats["running"]),
        ("等你", stats["attention"]),
        ("近 7 天完成", stats["recent"]),
    ):
        body.append(f'<div class="stat"><b>{value}</b><span>{esc(label)}</span></div>')
    body.append("</div>")

    center = snapshot["center"]
    cindy = snapshot["cindy"]
    center_line = (
        f"中心库 <code>{esc(center['path'])}</code>"
        + (f"（schema {center['schema']}）" if center.get("schema") is not None else "")
        + f" · 读取于 {clock_text(center['read_at'])}"
    )
    if not center["ok"]:
        center_line += f' · <b class="bad">读不到：{esc(center["error"])}</b>'
    cindy_line = (
        f"Cindy 主库 <code>{esc(cindy['name'] or '未找到 cindy-*.db')}</code>"
        + f" · mtime {esc(cindy['mtime_text'])}"
        + f" · 读取于 {clock_text(cindy['read_at'])}"
    )
    if not cindy["ok"]:
        cindy_error = esc(cindy["error"])
        cindy_line += f' · <b class="bad">读不到：{cindy_error}</b>'
    body.append(f'<p class="src">{center_line}<br>{cindy_line}<br>生成于 {clock_text(now)}</p>')
    if snapshot["issues"]:
        for issue in snapshot["issues"]:
            body.append(f'<p class="card bad">{esc(issue)}</p>')

    # ---- 需要你出手
    if stats["attention"]:
        body.append('<h2 id="attention">需要你出手 <span class="count">' + str(stats["attention"]) + "</span></h2>")
        body.append(
            '<p class="meta signals">'
            f"未答问题 {counts['questions']} · 工单受阻 {counts['blocked']} · "
            f"待验收超时 {counts['stale_review']} · 会话悬着 {counts['stuck']} · 真待合并 {counts['merge']}"
            "</p>"
        )

    for item in attention["questions"]:
        body.append('<div class="card signal">')
        body.append(f'<h3>未答问题 · {esc(item["ticket"])}</h3>')
        body.append(f'<p class="meta">{esc(item["title"])}</p>')
        body.append(f'<p class="quote">{esc(item["question"])}</p>')
        body.append(f'<p class="meta">{esc(item["project"])} · 等了 {human_seconds(item["waited"])}</p>')
        if item["session_id"]:
            body.append(f'<div class="row">{jump(item["session_id"], "去回答", opener_for(item["ticket"], item["title"]))}</div>')
        body.append("</div>")

    for item in attention["blocked"]:
        body.append('<div class="card signal">')
        body.append(f'<h3>工单受阻 · {esc(item["ticket"])}</h3>')
        body.append(f'<p class="meta">{esc(item["title"])}</p>')
        body.append(f'<p class="meta">{esc(item["project"])} · 状态 blocked，等人拍板</p>')
        body.append(ticket_actions(item["ticket"], item["title"], item["session_id"], item["coordinator_id"]))
        body.append("</div>")

    for item in attention["stale_review"]:
        body.append('<div class="card signal">')
        body.append(f'<h3>待验收超时 · {esc(item["ticket"])}</h3>')
        body.append(f'<p class="meta">{esc(item["title"])}</p>')
        body.append(
            f'<p class="meta">{esc(item["project"])} · 提交 {esc(item["submission"][:8])} 后 '
            f'{human_seconds(item["waited"])} 没有 review 记录</p>'
        )
        body.append(ticket_actions(item["ticket"], item["title"], item["session_id"], item["coordinator_id"]))
        body.append("</div>")

    if attention["stuck"]:
        longest = attention["stuck"][0]["waited"]
        today_key = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        today = [item for item in attention["stuck"] if datetime.fromtimestamp(item["at"]).strftime("%Y-%m-%d") == today_key]
        today_ids = {item["id"] for item in today}
        older = [item for item in attention["stuck"] if item["id"] not in today_ids]
        body.append('<div class="card signal">')
        body.append(f'<h3>会话悬着（推断）· {len(attention["stuck"])} 个</h3>')
        body.append(
            f'<p class="meta">最后一句是用户发、agent 没接上，最久 {human_seconds(longest)}；'
            f'其中今天起来的 {len(today)} 个，更早的 {len(older)} 个。'
            "这一条是推断，不是结论：可能是已经放弃的旧会话（归档口径待定，见 PRD D4）。</p>"
        )
        for label, group, is_open in (("今天起来的", today, True), ("更早的", older, False)):
            if not group:
                continue
            body.append(
                f'<details{" open" if is_open else ""}><summary>{label} {len(group)} 个</summary><ul class="list">'
            )
            for item in group:
                preview = f'<span class="meta">「{esc(item["session_head"])}」</span>' if item["session_head"] else ""
                body.append(
                    f'<li><a href="cindy://session/{esc(item["id"])}">{esc(collapse(item["title"], PREVIEW_LIMIT))}</a> '
                    f'<span class="meta">悬了 {human_seconds(item["waited"])}</span> {preview}</li>'
                )
            body.append("</ul></details>")
        body.append("</div>")

    for item in attention["merge_pending"]:
        body.append('<div class="card signal">')
        body.append(f'<h3>真待合并 · {esc(item["ticket"])} <span class="tag warn">待合并</span></h3>')
        body.append(f'<p class="meta">{esc(item["title"])}</p>')
        body.append(
            f'<p class="meta">{esc(item["project_name"])} · 分支 ticket/{esc(item["ticket"])} '
            f'tip {esc((item["tip"] or "")[:8])} = head {esc(item["head_sha"][:8])}</p>'
        )
        body.append("</div>")

    # ---- 正在跑
    body.append('<h2 id="running">正在跑 <span class="count">' + str(len(snapshot["running"])) + "</span></h2>")
    if snapshot["running"]:
        for item in snapshot["running"]:
            body.append('<div class="card">')
            body.append(f'<h3>{esc(item["ticket"])} · {esc(item["title"])}</h3>')
            body.append(
                f'<p class="meta">{esc(item["project_name"])} · 执行中 · 尝试 {esc(item["attempt"][:8])} · '
                f'跑了 {human_seconds(item["elapsed"])}</p>'
            )
            body.append(
                f'<p class="meta">执行会话「{esc(item["session_title"] or "未记录")}」'
                + (("（这一轮在跑）" if item["session_running"] else "（这一轮已收尾）") if item["session_title"] else "")
                + f' · 最后一条 {esc(item["session_head"])}</p>'
            )
            body.append(ticket_actions(item["ticket"], item["title"], item["session_id"], item["coordinator_id"]))
            body.append("</div>")
    else:
        body.append('<p class="empty">没有在跑的工单。</p>')

    if snapshot["sessions_only"]:
        body.append('<div class="card">')
        body.append(f'<h3>会话在跑（没有对应的在途工单）· {len(snapshot["sessions_only"])} 个</h3>')
        body.append('<ul class="list">')
        for item in snapshot["sessions_only"]:
            body.append(
                f'<li><a href="cindy://session/{esc(item["id"])}">{esc(collapse(item["title"], PREVIEW_LIMIT))}</a> '
                f'<span class="meta">已跑 {human_seconds(item["elapsed"])}</span></li>'
            )
        body.append("</ul></div>")

    # ---- 已验收未合
    body.append('<h2 id="unmerged">已验收未合</h2>')
    if snapshot["merge_pending"]:
        body.append('<p class="meta">只列真待合并：分支在、且 tip 等于最新一条提交的 head_sha。</p>')
        for item in snapshot["merge_pending"]:
            body.append('<div class="card">')
            body.append(f'<h3>{esc(item["ticket"])} · {esc(item["title"])}</h3>')
            body.append(
                f'<p class="meta">{esc(item["project_name"])} · 分支 tip {esc((item["tip"] or "")[:8])} · '
                f'提交 head {esc(item["head_sha"][:8])}</p>'
            )
            body.append("</div>")
    else:
        history_total = sum(len(items) for items in snapshot["history"].values())
        body.append(
            '<p class="empty">没有真待合并的工单。'
            + (f"历史上直做完成、没有合并记录的有 {history_total} 张，折叠在各项目下面。" if history_total else "")
            + "</p>"
        )

    # ---- 项目分组
    body.append('<h2 id="projects">项目分组</h2>')
    for card in snapshot["projects"]:
        body.append('<div class="card">')
        body.append(f'<h3>{esc(card["name"])} <span class="meta">{esc(card["id"])}</span></h3>')
        body.append(
            f'<p class="meta">在途 {card["inflight"]} · 累计已验收 {card["accepted"]} · '
            f'近 7 天完成 {card["recent"]}</p>'
        )
        if card["round"]:
            goals = card["round"]["本轮目标"]
            ends = card["round"]["结束条件"]
            line = f'本轮目标 {goals[0]} 条（达成 {goals[1]}）· 结束条件 {ends[0]} 条（达成 {ends[1]}）'
            if goals[2] or ends[2]:
                line += f" · 未标注 {goals[2] + ends[2]} 条"
            body.append(f'<p class="meta">{esc(line)}</p>')
        if card["history"]:
            body.append(f'<details><summary>历史直做 {len(card["history"])} 张</summary><ul class="list">')
            for item in card["history"]:
                body.append(
                    f'<li>{esc(item["ticket"])} · {esc(collapse(item["title"], 48))} '
                    f'<span class="tag">{esc(item["reason"])}</span></li>'
                )
            body.append("</ul></details>")
        body.append("</div>")

    # ---- 最近完成
    body.append('<h2 id="recent">最近完成</h2>')
    if snapshot["recent"]:
        for group in snapshot["recent"]:
            body.append(f'<h3 class="day">{esc(group["day"])} · {len(group["items"])} 张</h3>')
            body.append('<ul class="list">')
            for item in group["items"]:
                tail = f' · 合并 {esc(item["commit"][:8])}' if item["commit"] else ""
                body.append(
                    f'<li>{esc(item["ticket"])} · {esc(collapse(item["title"], 48))} '
                    f'<span class="meta">{esc(item["project_name"] or item["project"])} · '
                    f"{clock_text(item['at'])}{tail}</span></li>"
                )
            body.append("</ul>")
    else:
        body.append('<p class="empty">近 7 天没有新的验收通过记录。</p>')

    body.append('<p class="foot">只读快照：不改工单、不发消息、不起服务。刷新请重新运行 tools/snapshot.py。</p>')
    body.append("</div>")

    return TEMPLATE.format(body="\n".join(body))


TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>项目进度面板</title>
<style>
*{{box-sizing:border-box}}
:root{{--bg:#f5f6f8;--card:#fff;--ink:#1a1d21;--dim:#5c6875;--line:#dfe3e8;--accent:#2b64d9;--warn:#a35a00;--danger:#b3261e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#131619;--card:#1b1f24;--ink:#e8ecf1;--dim:#9aa6b3;--line:#2b3238;--accent:#8aaeff;--warn:#e0a24c;--danger:#ff8377}}}}
body{{margin:0;padding:12px;background:var(--bg);color:var(--ink);font:16px/1.55 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;overflow-wrap:anywhere;word-break:break-word}}
h1{{font-size:20px;margin:4px 0 10px}} h2{{font-size:17px;margin:22px 0 8px}} h3{{font-size:15px;margin:0 0 4px}} h3.day{{font-size:15px;color:var(--dim);margin:12px 0 2px}}
.stats{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}}
.stat{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}} .stat b{{display:block;font-size:24px;line-height:1.2}} .stat span{{font-size:14px;color:var(--dim)}}
.src,.foot{{font-size:14px;color:var(--dim);line-height:1.5;margin:10px 0 0}} .src code{{font-size:14px;overflow-wrap:anywhere}} .foot{{margin-top:24px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:8px 0}}
.meta{{font-size:14px;color:var(--dim);margin:4px 0}} .quote{{font-size:15px;margin:6px 0;padding-left:10px;border-left:3px solid var(--line)}} .empty{{font-size:14px;color:var(--dim);margin:8px 0}}
.row{{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;align-items:center}}
.btn{{display:inline-block;font-size:14px;line-height:1.2;padding:8px 12px;border-radius:8px;border:1px solid var(--line);background:transparent;color:var(--accent);text-decoration:none}}
button.btn{{cursor:pointer;color:var(--ink);font-family:inherit}} .btn.off{{color:var(--dim);border-style:dashed}}
.tag{{display:inline-block;font-size:14px;padding:1px 8px;border-radius:999px;border:1px solid var(--line);color:var(--dim)}} .tag.warn{{color:var(--warn);border-color:var(--warn)}}
.count{{display:inline-block;min-width:22px;text-align:center;font-size:14px;padding:1px 8px;border-radius:999px;border:1px solid var(--line);color:var(--dim)}}
details{{margin:6px 0}} summary{{cursor:pointer;font-size:14px;color:var(--accent)}} .list{{margin:6px 0;padding-left:18px}} .list li{{font-size:14px;margin:5px 0}} .bad{{color:var(--danger)}} .signals{{margin:0 0 6px}}
</style>
</head>
<body>
{body}
<script>
document.addEventListener('click', function (event) {{
  var button = event.target.closest('[data-copy]');
  if (!button) {{ return; }}
  var text = button.getAttribute('data-copy');
  var restore = function () {{
    var label = button.textContent;
    button.textContent = '已复制';
    setTimeout(function () {{ button.textContent = label; }}, 1500);
  }};
  var legacy = function () {{
    var box = document.createElement('textarea');
    box.value = text;
    box.setAttribute('readonly', '');
    box.style.position = 'fixed';
    box.style.top = '0';
    box.style.left = '-9999px';
    document.body.appendChild(box);
    box.select();
    var ok = false;
    try {{ ok = document.execCommand('copy'); }} catch (error) {{ ok = false; }}
    document.body.removeChild(box);
    if (ok) {{ restore(); }} else {{ window.prompt('长按复制这段开场白', text); }}
  }};
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(text).then(restore, legacy);
  }} else {{
    legacy();
  }}
}});
</script>
</body>
</html>
"""


def collect(center_path=CENTER_DB, cindy_dir=None, now=None):
    """读两个库并算成快照。测试可以换路径进来。"""
    loaded = load_databases(center_path=center_path, cindy_dir=cindy_dir, now=now)
    return build_snapshot(loaded["center"], loaded["cindy"], loaded["now"])


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    snapshot = collect()
    page = render(snapshot)
    OUTPUT.write_text(page, encoding="utf-8")
    stats = snapshot["stats"]
    print(f"写入 {OUTPUT}（{len(page)} 字符）")
    print(f"中心库 {snapshot['center']['path']} · schema {snapshot['center'].get('schema')} · 只读打开")
    print(
        "Cindy 主库 {} · mtime {} · 只读打开".format(
            snapshot["cindy"]["name"] or "未找到",
            snapshot["cindy"]["mtime_text"],
        )
    )
    print(
        "项目 {} · 在跑 {} · 等你 {} · 近 7 天完成 {}".format(
            stats["projects"], stats["running"], stats["attention"], stats["recent"]
        )
    )
    for issue in snapshot["issues"]:
        print("读不到：" + issue)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
