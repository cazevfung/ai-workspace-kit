#!/usr/bin/env python3
"""progress-export.json（schemaVersion 3）→ 样张吃的那份 FEED 模型。

只读：这个模块只打开一个文件读字节，不写盘、不起子进程、不发网络请求。

导出物是**按状态分组**的（`running` / `pool` / `historical-direct` …），而样张是**一卡一项目**。
所以这里做三件事：

1. 把 `groups[].tasks[]` 按 `task.project` 归到项目卡；
2. 把只有扁平 `tickets` 的组（池子）按 `ticket.project` 归进同一个项目的「还没人认领」块；
3. 用每张单所在组的 key 与它的 `mergeState` 算五步轨道，并标出默认可见的单
   （在途的、或 24 小时内动过的）——**全部单都在**，折叠只是默认值，页面的「展开」靠它。

组的 key → 五步走到第几格（0 起），照鸿蒙 viewmodel 的 `reachedOf()` 与 `stepsUpTo()`：
`queued`/`pool` 0、`running`/`needs-you` 2、`review` 3、已验收那几类 4（最后一格由 mergeState 覆写）。
**不认识的组 key 一条都不显示**（CD-019）：以前兜底成「在途、第 3 格」，导出物每加一个新组，
屏上就凭空多出一行没人认识的单，还带着一个假的当前步。现在按「不认识 = 不画」处理。

取消单（CD-019，导出物的顶层 `cancelled`，CH-094 落的那一段）单独成段：五步全灰、没有 resolve、
行尾一格留给绿 circle-slash。它们不进任何项目卡，也不算进卡头两个计数——与卡流是两条不同的账。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 3

#: 「24 小时内动过」的窗口
RECENT_WINDOW_MS = 24 * 60 * 60 * 1000
#: 卡内主任务默认露几个
TASKS_SHOWN = 3
#: 池子默认露几条
POOL_SHOWN = 2

#: 组 key →（走到第几格 0 起，是不是「在途」组）。在途的单默认全露。
GROUPS = {
    "needs-you": (2, True),
    "review": (3, True),
    "running": (2, True),
    "queued": (0, False),
    "pool": (0, False),
    "unmerged": (4, False),
    "merge-unlanded": (4, False),
    "merged-unrecorded": (4, False),
    "historical-direct": (4, False),
}

MERGE_NOTE = {
    "landed-unrecorded": "已在 master 里，但库里没有合并记录",
    "recorded-unlanded": "库里记了合并，但那个提交不在 master 里",
}

#: 这两种合并态不出 resolve：没有下一个人要动手。
#: `recorded` —— 已合，账也记了；`landed-unrecorded` —— 代码已经在 master 里，
#: 只差库里那一行合并记录（账走 backfill 或豁免）。后者以前出 resolve，点下去叫的是
#: 一条早就没了的老会话：宿主弹「找不到这条任务」，浮窗却写「已交回：已打开 …」（CD-022）。
#: 判就写在这里一处，页面不另判一遍；JS 侧（app/feed.mjs）逐字一致，黄金夹具钉着。
SETTLED_MERGE_STATES = ("recorded", "landed-unrecorded")


def has_resolve(merge_state: str) -> bool:
    """这张单该不该出 resolve（判据只有合并态，与组 key 无关）。"""
    return (merge_state or "") not in SETTLED_MERGE_STATES

#: 取消单的五步：**五格全灰、没有当前步**（与鸿蒙 `cancelledSteps()` 同一口径）。
#: 不用 `steps_for("queued")`：那会把第一格画成橙的当前步，读起来像「马上要开跑」。
CANCELLED_STEPS = ["todo"] * 5


class ExportError(Exception):
    """导出物读不到 / 不是 schemaVersion 3 / 解析失败 / 形状不对。"""


def fingerprint(path) -> str:
    """文件的「变没变」指纹：mtime + 长度。原子替换写会让 mtime 动。"""
    stat = os.stat(path)
    return "%d-%d" % (stat.st_mtime_ns, stat.st_size)


def read_payload(path) -> dict:
    """读一份完整的 JSON。不重试、不拼接、不建缓存文件。"""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ExportError("读不到导出物：%s" % exc) from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportError("导出物不是完整的 JSON：%s" % exc) from exc


def clock_of(generated_at: str) -> str:
    """「2026-11-23T08:58:48+08:00」→「08:58:48」。取不到就给空串，不拿本机时钟顶替。"""
    if not isinstance(generated_at, str) or "T" not in generated_at:
        return ""
    rest = generated_at.split("T", 1)[1]
    return rest[:8] if len(rest) >= 8 else ""


def _activity_ms(text) -> int | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return int(moment.timestamp() * 1000)


def steps_for(group_key: str, merge_state: str) -> list | None:
    """五步轨道（每格一个状态）。最后一格由 mergeState 覆写，与鸿蒙 `withMergeState()` 同一套。

    不认识的组 key 返回 `None`：调用方据此整条不画，不再兜底成「在途、第 3 格」。
    """
    if group_key not in GROUPS:
        return None
    reached = GROUPS[group_key][0]
    if reached >= 4:
        if merge_state == "recorded":
            return ["done"] * 5
        if merge_state == "landed-unrecorded":
            return ["done"] * 4 + ["landed"]
        if merge_state == "recorded-unlanded":
            return ["done"] * 4 + ["unlanded"]
        # not-merged / 空串：停在合并那一格（当前步），不是走完
        return ["done"] * 4 + ["current"]
    if reached == 3:
        # 待验收：验收那一格是当前步，下一格（合并）标 next——同样呼吸
        return ["done"] * 3 + ["current", "next"]
    if reached == 2:
        return ["done"] * 2 + ["current", "todo", "todo"]
    return ["current", "todo", "todo", "todo", "todo"]


def _ticket(entry: dict, group_key: str) -> dict | None:
    """一条工单行；组 key 不认识时返回 None（整条不画）。"""
    steps = steps_for(group_key, entry.get("mergeState") or "")
    if steps is None:
        return None
    merge_state = entry.get("mergeState") or ""
    session_id = str(entry.get("ticketSessionId") or "")
    return {
        "_group": group_key,
        "id": str(entry.get("id") or ""),
        "title": str(entry.get("title") or ""),
        "steps": steps,
        # 出不出 resolve 判在这里（CD-022）：已合（recorded）与「已在 master、库里没记录」
        # 都不出——它们没有下一个人要动手，点了只会去叫一条早就不在的会话。
        "resolve": has_resolve(merge_state),
        # 没有会话就不画箭头（池子里的单）
        "arrow": bool(session_id),
        # 行尾两枚图标都要一个可去的目标：箭头与取消行的绿 circle-slash 都靠它
        "sessionId": session_id,
        # 会话还在不在（CD-021）：**这一条只有 JS 那条路能答**。browser 这条路（`/api/feed`）
        # 只读导出物、不读宿主的会话库，所以这里永远是「不知道」——不写 `sessionGone` 这个键，
        # 箭头照画、resolve 照旧交回（与 JS 侧传 `liveSessions=null` 时逐字一致，黄金夹具钉着）。
        # 桌面浮窗读会话库那一支在 `app/sessions.mjs`。
        "note": MERGE_NOTE.get(merge_state, ""),
        "_at": entry.get("lastActivityAt") or "",
    }


def _order(items: list[dict]) -> list[dict]:
    """按最近活动降序；并列时按 id 升序。没时间的放最后。"""
    def key(item):
        ms = _activity_ms(item.get("_at"))
        return (1, 0, item.get("id") or "") if ms is None else (0, -ms, item.get("id") or "")
    return sorted(items, key=key)


def _mark_shown(tickets: list[dict], group_key: str, now_ms: int) -> int:
    """标出默认露哪些单：在途那组的全露；其余只露 24 小时内动过的。

    不能把「默认可见」算成前 N 个：最近动过的单可能夹在老单中间，截前缀会把该露的藏起来、
    该藏的露出来。所以每条单自己带一个 `show` 标记，页面折叠时按它过滤、展开时全给，
    顺序始终是同一份排好的顺序（折叠是过滤，不是另一种排法）。
    一条都不符合时至少留最新那一条（鸿蒙 CH-074 的兜底）。
    """
    in_flight = GROUPS[group_key][1]
    shown = 0
    for index, ticket in enumerate(tickets):
        ms = _activity_ms(ticket.get("_at"))
        visible = in_flight or index == 0 or (ms is not None and now_ms - ms <= RECENT_WINDOW_MS)
        ticket["show"] = visible
        if visible:
            shown += 1
    return shown


def cancelled_note(project: str, cancelled_at) -> str:
    """取消行的行下那一句：`项目 · 取消 11-22`。

    只拿时刻字符串里的 `MM-DD`，**不做时区换算**：两端（Python / JS）必须逐字相等，
    而时区换算正是两边最容易出现一整天偏差的地方。读不到时刻就不写，也不编一个日期。
    """
    when = ""
    if isinstance(cancelled_at, str) and "T" in cancelled_at and len(cancelled_at) >= 10:
        when = "取消 " + cancelled_at[5:10]
    name = str(project or "")
    if not name:
        return when
    return name if not when else name + " · " + when


def cancelled_of(payload: dict) -> list[dict]:
    """导出物顶层 `cancelled` → 页面那一段取消单。

    读不到（旧导出物没有这个键 / 不是数组）就是空数组——**页面上什么都不多、也不报错**。
    CD-019 之前那份导出物没有这个键，而那一刻页面仍要照常可用。

    排序不在这里做：导出器已经按「取消时刻新的在前」排好了，重排一次等于把两端的口径分叉。
    """
    raw = payload.get("cancelled")
    if not isinstance(raw, list):
        return []
    rows = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        session_id = str(entry.get("sessionId") or "")
        rows.append({
            "id": str(entry.get("id") or ""),
            "title": str(entry.get("title") or ""),
            "project": str(entry.get("project") or ""),
            "cancelledAt": str(entry.get("cancelledAt") or ""),
            "steps": list(CANCELLED_STEPS),
            # 取消单没有 resolve（不需要再交给谁收口），也不在卡头两个计数里
            "resolve": False,
            "arrow": bool(session_id),
            "sessionId": session_id,
            "note": cancelled_note(entry.get("project"), entry.get("cancelledAt")),
        })
    return rows


def roots_of(payload: dict) -> dict:
    """项目名 / 项目 id → 项目根（导出物 `projects[].root`，CH-093 起有）。

    两个键都记：卡是按**单自己的 project 名**分的，而旧的导出物里那一栏有时是 id。
    读不到就没有这个键，卡上的 root 是空串——**不拿当前目录、也不猜一个路径**，
    因为那会让浮窗在错的地方开一条会话。
    """
    roots = {}
    for entry in payload.get("projects") or []:
        if not isinstance(entry, dict):
            continue
        root = str(entry.get("root") or "")
        if not root:
            continue
        for key in (entry.get("name"), entry.get("id")):
            if isinstance(key, str) and key:
                roots.setdefault(key, root)
    return roots


def normalize(payload: dict, now_ms: int) -> dict:
    """导出物 → FEED。返回的就是页面直接吃的那一份。"""
    if not isinstance(payload, dict):
        raise ExportError("导出物顶层不是一个对象")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise ExportError("导出物 schemaVersion 是 %r，只会读 %d"
                          % (payload.get("schemaVersion"), SCHEMA_VERSION))

    buckets: dict[str, dict] = {}
    roots = roots_of(payload)

    def bucket(name: str) -> dict:
        if name not in buckets:
            buckets[name] = {"name": name, "tasks": [], "pool": []}
        return buckets[name]

    for group in payload.get("groups") or []:
        if not isinstance(group, dict):
            continue
        group_key = str(group.get("key") or "")
        # 不认识的组 key：整组不画（CD-019）。以前兜底成「在途、第 3 格」，屏上凭空多一行。
        if group_key not in GROUPS:
            continue
        for task in group.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            # 一张主任务名下的单可能来自不同项目（会话跨项目跑过）：按**单自己的 project** 分卡，
            # 与鸿蒙 `draftOf(drafts, ticket.project)` 同一口径。按 task.project 一刀切会把别项目的单
            # 挂到这张卡上，卡头张数也就与文件里按项目数出来的对不上。
            by_project = {}
            for entry in task.get("tickets") or []:
                if not isinstance(entry, dict):
                    continue
                row = _ticket(entry, group_key)
                if row is None:
                    continue
                name = str(entry.get("project") or task.get("project") or "")
                by_project.setdefault(name, []).append(row)
            for project, tickets in by_project.items():
                tickets = _order(tickets)
                board = bucket(project)
                board["tasks"].append({
                    "key": str(task.get("sessionId") or "") or ("task-%d" % len(board["tasks"])),
                    "name": str(task.get("title") or ""),
                    "tickets": tickets,
                    "shown": _mark_shown(tickets, group_key, now_ms),
                    "_at": str(task.get("lastActivityAt") or ""),
                })
        for entry in group.get("tickets") or []:
            if not isinstance(entry, dict):
                continue
            row = _ticket(entry, group_key)
            if row is None:
                continue
            bucket(str(entry.get("project") or ""))["pool"].append(row)

    projects = []
    for name, board in buckets.items():
        tasks = sorted(board["tasks"],
                       key=lambda task: _activity_ms(task.get("_at")) or 0, reverse=True)
        pool = _order(board["pool"])
        total = sum(len(task["tickets"]) for task in tasks) + len(pool)
        latest = 0
        for task in tasks:
            latest = max(latest, _activity_ms(task.get("_at")) or 0)
            task.pop("_at", None)
        for ticket in pool:
            latest = max(latest, _activity_ms(ticket.get("_at")) or 0)
            ticket.pop("_at", None)
        review = 0
        needs_you = 0
        for task in tasks:
            for ticket in task["tickets"]:
                ticket.pop("_at", None)
                group = ticket.pop("_group", "")
                if group == "review":
                    review += 1
                elif group == "needs-you":
                    needs_you += 1
        for ticket in pool:
            ticket.pop("_at", None)
            group = ticket.pop("_group", "")
            if group == "review":
                review += 1
            elif group == "needs-you":
                needs_you += 1
        projects.append({
            "name": name,
            "total": total,
            # 卡头那两个数数的是**行**：屏上画几张「待验收」的单，卡头就写几，不另算一份
            "review": review,
            "needsYou": needs_you,
            # resolve 在项目里开会话时用得到（CH-093 的导出物带上了 root）；读不到就是空串
            "root": roots.get(name, ""),
            "tasks": tasks,
            "shownTasks": min(TASKS_SHOWN, len(tasks)),
            "pool": pool,
            "shownPool": min(POOL_SHOWN, len(pool)),
            "_latest": latest,
        })

    projects.sort(key=lambda project: -project["_latest"])
    for project in projects:
        project.pop("_latest", None)

    return {
        "generatedAt": str(payload.get("generatedAt") or ""),
        "generatedClock": clock_of(payload.get("generatedAt")),
        "schemaVersion": SCHEMA_VERSION,
        "projects": projects,
        # 取消单：单独一段，不进任何项目卡、也不算进卡头两个计数。读不到就是空数组。
        "cancelled": cancelled_of(payload),
    }



def _cli(argv=None) -> int:
    """把一份导出物归一后打印/写出来。黄金夹具就是用它生成的：

        python tools/exportfeed.py tests/fixtures/export-golden.json --now 1795176000 --out tests/fixtures/export-golden.expected.json

    `--now` 是「把哪一刻当作现在」（epoch 秒），折叠的 24 小时窗口以它为基准；
    不传就用当前时间。这个入口只读、只打印/写一个文件，不改任何既有行为。
    """
    parser = argparse.ArgumentParser(description="progress-export.json → 样张 FEED（只读）")
    parser.add_argument("export", help="导出物路径")
    parser.add_argument("--now", type=float, default=None, help="当作现在的那一刻（epoch 秒），默认当前时间")
    parser.add_argument("--out", default=None, help="写到哪个文件；不传就打印到 stdout")
    args = parser.parse_args(argv)

    now_ms = int((args.now if args.now is not None else time.time()) * 1000)
    feed = normalize(read_payload(args.export), now_ms)
    text = json.dumps(feed, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
