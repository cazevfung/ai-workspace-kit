#!/usr/bin/env python3
"""只读取数层：两个 SQLite 库的只读打开、时间换算、在跑与待合并的判据。

从 tools/snapshot.py 抽出来，给实时服务（tools/serve.py）与快照脚本共用。
判据写死在下面的常量里（PRD §4.2、4.3、4.4）：在跑按列比时间、卡住 10 分钟、
待验收 4 小时、待合并要比分支 tip。

只读：两个库都用 URI `file:<路径>?mode=ro` 打开，连上立刻 PRAGMA query_only=ON；
代码里只有 SELECT。不复制库、不做 WAL checkpoint、不 ATTACH 别的库。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

CENTER_DB = Path("{{WORKSPACE}}/.data/task-center.sqlite3")

# PRD 4.5 / task_center.core.ACTIVE_TICKET_STATES：在途四态
ACTIVE_TICKET_STATES = ("dispatching", "running", "awaiting_review", "blocked")
# PRD 4.2「像卡住了」与 4.3 S4：10 分钟
STUCK_AFTER_S = 10 * 60
# PRD 4.3 / 4.18（0.7）：S4 的上限 24 小时；一天没动的是旧对话，不是今天的待办
STUCK_MAX_AGE_S = 24 * 3600
# PRD 4.2（0.4）：活跃轮次超过 6 小时就不再算在跑，标「状态可疑」
RUNNING_MAX_AGE_S = 6 * 3600
# PRD 4.3 S3：4 小时
REVIEW_TIMEOUT_S = 4 * 3600
# PRD 4.5：最近 7 天
RECENT_DAYS = 7
# PRD 4.6：复制开场白的模板，逐字使用
COPY_TEMPLATE = "看 {ticket} {title}，现在到哪一步？我有个想法："
PREVIEW_LIMIT = 60

CENTER_QUERIES = {
    "projects": "SELECT id, name, root FROM projects",
    # criteria 是这张单的验收条件清单（JSON 数组），PRD §4.11「10 条待验收」的分母只能从它来；
    # 4.1 的 tickets 列清单里漏了这一列，实现的取舍见提交里的 risks。
    "tickets": (
        "SELECT id, project_id, title, state, current_attempt, created_at, criteria FROM tickets"
    ),
    "attempts": (
        "SELECT id, ticket_id, state, session_id, coordinator_session_id, created_at FROM attempts"
    ),
    "submissions": "SELECT id, attempt_id, created_at, head_sha FROM submissions",
    "reviews": "SELECT submission_id, verdict, checks, reason, created_at FROM reviews",
    "merges": "SELECT ticket_id, commit_sha, created_at FROM merges",
    "questions": (
        "SELECT id, ticket_id, attempt_id, question, state, asked_at FROM questions"
    ),
}

# PRD 4.1 的会话列；「最后一句是谁说的」只用 list_preview 与 list_preview_role，不读 messages 正文。
# working_dir 用来把会话归到项目（含工作树）；PRD 4.1 的会话行里有这一列。
# PRD 4.2「空闲」的反例写着「把已归档会话算进来（D4 未定前不显示归档）」，所以这里读 status，
# 判定只认 status='active'。这一列还没进 4.1 的清单，留给 D4 定。
# PRD 4.2（0.5）：`active_turn_pid` 不再参与判定——本机实测 1812 个会话该列全为 NULL（0 个非空），
# 拿它当必需条件会把所有真在跑的会话都判成「状态可疑」，所以只读时间列。
SESSIONS_SQL = (
    "SELECT id, title, status, working_dir, list_preview, list_preview_role, active_turn_started_at, "
    "last_turn_ended_at, last_activity_at FROM sessions"
)


# --------------------------------------------------------------------------- 时间


def to_seconds(value):
    """把两种时间列统一换成 epoch 秒。

    中心库的时间列是 ISO 文本；Cindy 库的时间列是 epoch 毫秒。把毫秒当秒会算出 1970 年，
    把秒当毫秒会算出五万年以后——所以先按量级判单位，再解析文本。
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if abs(number) >= 1e11:  # 2026 年的秒值约 1.8e9，毫秒值约 1.8e12
            return number / 1000.0
        return number
    moment = str(value).strip()
    if not moment:
        return None
    try:
        parsed = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def human_seconds(seconds):
    if seconds is None:
        return "未知"
    total = int(max(0, seconds))
    if total < 60:
        return "不到 1 分钟"
    if total < 3600:
        return f"{total // 60} 分钟"
    if total < 86400:
        return f"{total // 3600} 小时 {(total % 3600) // 60} 分"
    return f"{total // 86400} 天 {(total % 86400) // 3600} 小时"


def clock_text(seconds):
    if seconds is None:
        return "时间未知"
    return datetime.fromtimestamp(seconds).strftime("%m-%d %H:%M")


def day_text(seconds):
    if seconds is None:
        return "日期未知"
    moment = datetime.fromtimestamp(seconds)
    names = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    return f"{moment.strftime('%Y-%m-%d')} {names[moment.weekday()]}"


# --------------------------------------------------------------------------- 连接


def connect_readonly(path):
    """file:...?mode=ro 打开，连上立刻 PRAGMA query_only=ON。"""
    uri = "file:{}?mode=ro".format(Path(path).as_posix())
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _mtime(item):
    try:
        return item.stat().st_mtime
    except OSError:
        return 0.0


def find_cindy_db(directory):
    """只在给定这一层找 cindy-*.db，按 mtime 取最新的一个；不递归进 .dev-instances。"""
    folder = Path(directory)
    try:
        candidates = [item for item in folder.glob("cindy-*.db") if item.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=_mtime)


def _read_rows(conn, queries):
    conn.execute("BEGIN")
    try:
        rows = {name: [dict(row) for row in conn.execute(sql)] for name, sql in queries.items()}
    except sqlite3.Error:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return rows


def load_center(path, now):
    info = {"path": str(path), "ok": False, "error": None, "read_at": now, "rows": None, "schema": None}
    if not Path(path).is_file():
        info["error"] = "文件不存在"
        return info
    conn = None
    try:
        conn = connect_readonly(path)
        info["schema"] = conn.execute("PRAGMA user_version").fetchone()[0]
        info["rows"] = _read_rows(conn, CENTER_QUERIES)
    except sqlite3.Error as exc:
        info["error"] = str(exc)
    else:
        info["ok"] = True
    finally:
        if conn is not None:
            conn.close()
    return info


def load_cindy(path, now):
    info = {
        "path": None,
        "name": None,
        "mtime": None,
        "mtime_text": "无",
        "ok": False,
        "error": None,
        "read_at": now,
        "rows": None,
    }
    if path is None:
        info["error"] = "目录里没有 cindy-*.db"
        return info
    info["path"] = str(path)
    info["name"] = Path(path).name
    try:
        stamp = Path(path).stat().st_mtime
        info["mtime"] = stamp
        info["mtime_text"] = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M:%S")
    except OSError as exc:
        info["error"] = str(exc)
        return info
    conn = None
    try:
        conn = connect_readonly(path)
        info["rows"] = {"sessions": [dict(row) for row in conn.execute(SESSIONS_SQL)]}
    except sqlite3.Error as exc:
        info["error"] = str(exc)
    else:
        info["ok"] = True
    finally:
        if conn is not None:
            conn.close()
    return info


# --------------------------------------------------------------------------- 判定


def session_running(session, now=None):
    """PRD 4.2：active_turn_started_at 非空，且 last_turn_ended_at 为空或更早。

    方向最容易写反：started 更大才是新的一轮已经开始；ended 更大说明这一轮已收尾。
    这一条只管时间戳的方向（AC03）；0.4 起主视图要的是 session_live，多一道 6 小时门槛（AC14）。
    """
    return turn_open(session)


def turn_open(session):
    """活跃轮次是否「看起来还没收尾」：started 非空，且没有 ended 或 ended 更早。"""
    started = to_seconds(session.get("active_turn_started_at"))
    if started is None:
        return False
    ended = to_seconds(session.get("last_turn_ended_at"))
    return ended is None or ended < started


def session_live(session, now):
    """PRD 4.2（0.5）：三条全成立才算「在跑」。

    1. `active_turn_started_at` 非空
    2. `last_turn_ended_at` 晚于它「不成立」（这一轮还没收尾）
    3. 开始时间在 6 小时内

    2026-11-20 实测：`[Schedule] 巡逻评论区` 的 `active_turn_started_at` 是 99 天前。
    只按「非空 + ended 更早」判，这种没清标记的旧会话会永远显示在跑。

    0.5 去掉了 `active_turn_pid` 条件：本机 1812 个会话该列全是 NULL，加了它等于把所有
    真在跑的会话都判成「状态可疑」。6 小时这道门槛已经能挡住僵尸会话。
    """
    if session.get("status") is not None and not session_active(session):
        return False
    if not turn_open(session):
        return False
    started = to_seconds(session.get("active_turn_started_at"))
    if started is None or now - started > RUNNING_MAX_AGE_S:
        return False
    return True


def session_suspect(session, now):
    """字段说有活轮、但已经超过 6 小时：标「状态可疑」，不当成在跑（PRD 4.2）。

    只有「轮次看起来还没收尾」才可疑：ended 晚于 started 是自洽的收尾，属等人/空闲，
    不是可疑（PRD 4.2 第一行的反例：0e85a9e0 是界面开着在等人）。
    """
    if session.get("status") is not None and not session_active(session):
        return False
    return turn_open(session) and not session_live(session, now)


def session_active(session):
    """D4 未定前只算 status='active'：归档会话不进「空闲」，也不进 S4。

    PRD 4.2「空闲」的反例写着「把已归档会话算进来（D4 未定前不显示归档）」。
    """
    return session.get("status") == "active"


def session_idle(session):
    """PRD 4.2「空闲」：没有活跃轮次，且会话仍在册（status='active'）。

    工单侧那个 `running` 尝试由「正在跑」单独标一次（4.2 第三行：两个来源各标一次），
    不在会话判据里重复算。
    """
    return session_active(session) and not session_running(session)


def session_stuck(session, now):
    """PRD 4.2 / 4.3 S4 / 4.18：空闲、最后一句是用户发的、距 last_activity_at 超过 10 分钟且不到 24 小时。

    上限是 0.7 加的：只写下限时，1–22 天前的旧会话会把「等你」那一堆占满。
    """
    if session.get("list_preview_role") != "user":
        return False
    if not session_idle(session):
        return False
    last = to_seconds(session.get("last_activity_at"))
    if last is None:
        return False
    waited = now - last
    return STUCK_AFTER_S < waited <= STUCK_MAX_AGE_S


def classify_merge(head_sha, repo_ok, branch_tip):
    """PRD 4.4：全部成立才算真待合并，否则进历史直做并给出原因。"""
    if not repo_ok:
        return "history", "无仓库"
    if not head_sha:
        return "history", "无 head_sha"
    if branch_tip is None:
        return "history", "无分支"
    if branch_tip.lower() != head_sha.lower():
        return "history", "tip 不一致"
    return "pending", None


def is_git_repo(root):
    if not root:
        return False
    try:
        return (Path(root) / ".git").exists()
    except OSError:
        return False


def git_branch_tip(root, ticket_id):
    """git -C <项目根> rev-parse --verify refs/heads/ticket/<工单号>；非零退出就是没有分支。"""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "refs/heads/ticket/" + ticket_id],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None


def git_branch_tips(root):
    """一次列出 refs/heads/ticket/* 的 tip：每个仓库只开一个 git 进程。

    实时服务每 2 秒判一次真待合并，逐张单 rev-parse 会开出几十个进程；
    这里一次列全，结果只在本轮计算里用，下一次请求重新问 git。
    """
    try:
        done = subprocess.run(
            [
                "git", "-C", str(root), "for-each-ref",
                "--format=%(objectname) %(refname)", "refs/heads/ticket/",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if done.returncode != 0:
        return {}
    prefix = "refs/heads/ticket/"
    tips = {}
    for line in done.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        sha, refname = parts
        if refname.startswith(prefix):
            tips[refname[len(prefix):]] = sha
    return tips


def read_round(root):
    """PRD 4.5 轮次层：读 tasks/round.md 的「本轮目标」「结束条件」两节，只数勾选框。"""
    if not root:
        return None
    path = Path(root) / "tasks" / "round.md"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    sections = {"本轮目标": [0, 0, 0], "结束条件": [0, 0, 0]}  # 条数、达成、未标注
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            title = stripped[3:].strip()
            current = title if title in sections else None
            continue
        if current is None:
            continue
        if stripped.startswith("- [ ]") or stripped.startswith("- [x]") or stripped.startswith("- [X]"):
            sections[current][0] += 1
            if stripped[3:5].lower() == "[x":
                sections[current][1] += 1
        elif stripped.startswith("- ") or (stripped[:3].rstrip(".").isdigit() and stripped[2:4] == ". "):
            sections[current][2] += 1
    return sections


# --------------------------------------------------------------------------- 取数


def collapse(text, limit=None):
    """把多行压成一行。limit 省略时不截断：阻断原因的全文要留给 title（PRD §4.16）。"""
    flat = " ".join(str(text or "").split())
    if limit is None or len(flat) <= limit:
        return flat
    return flat[:limit].rstrip() + "…"


# --------------------------------------------------------------------------- 找库并读


def default_cindy_dir():
    """Cindy 主库所在目录：Windows 用户漫游目录下的 Cindy；只在这一次性目录里找。"""
    roaming = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
    return Path(roaming) / "Cindy"


def load_databases(center_path=CENTER_DB, cindy_dir=None, now=None):
    """读两个库的原始行。每次都重新找 Cindy 库、重新打开两个库，不做缓存。

    返回 {"now", "center", "cindy"}；读不到时 info 里 ok=False、error 写明原因，
    由调用方决定怎么展示，不在这里抛异常、不拿旧数据顶替。
    """
    moment = time.time() if now is None else now
    center = load_center(Path(center_path), moment)
    directory = Path(cindy_dir) if cindy_dir is not None else default_cindy_dir()
    cindy_path = find_cindy_db(directory)
    cindy = load_cindy(cindy_path, moment)
    return {"now": moment, "center": center, "cindy": cindy}
