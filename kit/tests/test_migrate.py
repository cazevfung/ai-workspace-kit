"""schema 3 → 8 的升级：只走已知的升级链，且拒绝时一个字节都不改。

正式库 `{{WORKSPACE}}/.data/task-center.sqlite3` 里已经有工单、尝试与验收记录，
不能因为多了一列或一张表就重建。这里自己合成旧版库（把后来才有的列与表都去掉，
写成冻结的 v3 / v5 / v6 形状），再对副本跑 `migrate`，逐条核对「版本变了、列和表多了、
行数没少、工单还读得出来」。

拒绝路径同样重要：`migrate` 不是「尽力而为」的修复工具。版本不认识、或文件根本
不是本工具的库，它必须什么都不做，让调用方去看清楚那是什么文件。
"""
import hashlib
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from task_center import Center
from task_center.db import SCHEMA_VERSION

# 冻结的 schema 3：与当前 SCHEMA 的差别只有 attempts.coordinator_session_id。
# 既然是历史形状，就不要跟着 task_center.db.SCHEMA 一起改。
V3_SCHEMA = """
CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, root TEXT NOT NULL UNIQUE);
CREATE TABLE tickets(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), title TEXT NOT NULL,
 deliverable TEXT NOT NULL,
 prd_path TEXT NOT NULL, prd_sha256 TEXT NOT NULL, criteria TEXT NOT NULL,
 revision INTEGER NOT NULL, state TEXT NOT NULL, current_attempt TEXT, created_at TEXT NOT NULL);
CREATE TABLE attempts(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id), revision INTEGER NOT NULL,
 prd_path TEXT NOT NULL, prd_sha256 TEXT NOT NULL,
 state TEXT NOT NULL, session_id TEXT, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX active_session ON attempts(session_id)
 WHERE session_id IS NOT NULL AND state IN ('running','submitted');
CREATE TABLE submissions(
 id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id),
 summary TEXT NOT NULL, artifacts TEXT NOT NULL, evidence TEXT NOT NULL, risks TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE reviews(
 id TEXT PRIMARY KEY, submission_id TEXT NOT NULL UNIQUE REFERENCES submissions(id),
 reviewer_session_id TEXT NOT NULL, verdict TEXT NOT NULL, checks TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT REFERENCES tickets(id),
 operation TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX events_ticket ON events(ticket_id, id);
CREATE INDEX tickets_project ON tickets(project_id, created_at);
CREATE TABLE requests(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL);
"""

TABLES = ("projects", "tickets", "attempts", "submissions", "reviews", "events", "requests")

# 冻结的 schema 5：有 merges、有 coordinator_session_id，还没有 questions/answers，
# active_session 谓词也不含 blocked。5 -> 6 那一步要改的就是这三处。
V5_SCHEMA = """
CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, root TEXT NOT NULL UNIQUE);
CREATE TABLE tickets(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), title TEXT NOT NULL,
 deliverable TEXT NOT NULL,
 prd_path TEXT NOT NULL, prd_sha256 TEXT NOT NULL, criteria TEXT NOT NULL,
 revision INTEGER NOT NULL, state TEXT NOT NULL, current_attempt TEXT, created_at TEXT NOT NULL);
CREATE TABLE attempts(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id), revision INTEGER NOT NULL,
 prd_path TEXT NOT NULL, prd_sha256 TEXT NOT NULL,
 state TEXT NOT NULL, session_id TEXT, coordinator_session_id TEXT, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX active_session ON attempts(session_id)
 WHERE session_id IS NOT NULL AND state IN ('running','submitted');
CREATE TABLE submissions(
 id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id),
 summary TEXT NOT NULL, artifacts TEXT NOT NULL, evidence TEXT NOT NULL, risks TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE reviews(
 id TEXT PRIMARY KEY, submission_id TEXT NOT NULL UNIQUE REFERENCES submissions(id),
 reviewer_session_id TEXT NOT NULL, verdict TEXT NOT NULL, checks TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT REFERENCES tickets(id),
 operation TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE merges(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL UNIQUE REFERENCES tickets(id),
 commit_sha TEXT NOT NULL, target_branch TEXT NOT NULL, tests TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX events_ticket ON events(ticket_id, id);
CREATE INDEX tickets_project ON tickets(project_id, created_at);
CREATE TABLE requests(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL);
"""

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_v3(path, project_root, stamp="2026-01-01T00:00:00+00:00"):
    """写一个内容完整的 v3 库：项目、工单、尝试、提交、验收、事件都有。"""
    prd_sha = hashlib.sha256((Path(project_root) / "PRD.md").read_bytes()).hexdigest()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(V3_SCHEMA)
        conn.execute("INSERT INTO projects VALUES ('p1','项目一',?)", (str(project_root),))
        conn.execute(
            "INSERT INTO tickets VALUES ('t1','p1','整理检查清单','一份清单','PRD.md',?,'[]',1,'running','a1',?)",
            (prd_sha, stamp))
        conn.execute(
            "INSERT INTO tickets VALUES ('t2','p1','另起一张','另一份清单','PRD.md',?,'[]',1,'ready',NULL,?)",
            (prd_sha, stamp))
        conn.execute("INSERT INTO attempts VALUES ('a1','t1',1,'PRD.md',?,'running','executor-1',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO submissions VALUES ('s1','a1','已交付','[]','[\"自检\"]','[]',?)", (stamp,))
        conn.execute("INSERT INTO reviews VALUES ('r1','s1','reviewer-1','accept','[]','逐项核对',?)", (stamp,))
        conn.execute("INSERT INTO events(ticket_id,operation,detail,created_at) VALUES ('t1','claim_ticket','{}',?)",
                     (stamp,))
        conn.execute("INSERT INTO events(ticket_id,operation,detail,created_at) VALUES ('t1','bind_session','{}',?)",
                     (stamp,))
        conn.execute("INSERT INTO requests VALUES ('req-1','fp','{}')")
        conn.execute("PRAGMA user_version=3")
        conn.commit()
    finally:
        conn.close()
    return path


def counts(path, tables=TABLES):
    conn = sqlite3.connect(path)
    try:
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
    finally:
        conn.close()


def build_v5(path, project_root, stamp="2026-01-01T00:00:00+00:00"):
    """写一个内容完整的 v5 库：一张 running 工单、一张已验收且已合并的工单。"""
    prd_sha = hashlib.sha256((Path(project_root) / "PRD.md").read_bytes()).hexdigest()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(V5_SCHEMA)
        conn.execute("INSERT INTO projects VALUES ('p1','项目一',?)", (str(project_root),))
        conn.execute("INSERT INTO tickets VALUES ('t1','p1','还在做','一份清单','PRD.md',?,'[]',1,'running','a1',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO tickets VALUES ('t2','p1','已经验收','另一份清单','PRD.md',?,'[]',1,'done','a2',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO attempts VALUES ('a1','t1',1,'PRD.md',?,'running','executor-1','coordinator-1',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO attempts VALUES ('a2','t2',1,'PRD.md',?,'accepted','executor-2','coordinator-1',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO merges VALUES ('m1','t2','abc1234','master','190 项全绿',?)", (stamp,))
        conn.execute("INSERT INTO events(ticket_id,operation,detail,created_at) VALUES ('t1','bind_session','{}',?)",
                     (stamp,))
        conn.execute("INSERT INTO requests VALUES ('req-1','fp','{}')")
        conn.execute("PRAGMA user_version=5")
        conn.commit()
    finally:
        conn.close()
    return path


V6_SCHEMA = """
CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, root TEXT NOT NULL UNIQUE);
CREATE TABLE tickets(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), title TEXT NOT NULL,
 deliverable TEXT NOT NULL,
 prd_path TEXT NOT NULL, prd_sha256 TEXT NOT NULL, criteria TEXT NOT NULL,
 revision INTEGER NOT NULL, state TEXT NOT NULL, current_attempt TEXT, created_at TEXT NOT NULL);
CREATE TABLE attempts(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id), revision INTEGER NOT NULL,
 prd_path TEXT NOT NULL, prd_sha256 TEXT NOT NULL,
 state TEXT NOT NULL, session_id TEXT, coordinator_session_id TEXT, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX active_session ON attempts(session_id)
 WHERE session_id IS NOT NULL AND state IN ('blocked','running','submitted');
CREATE TABLE submissions(
 id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id),
 summary TEXT NOT NULL, artifacts TEXT NOT NULL, evidence TEXT NOT NULL, risks TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE reviews(
 id TEXT PRIMARY KEY, submission_id TEXT NOT NULL UNIQUE REFERENCES submissions(id),
 reviewer_session_id TEXT NOT NULL, verdict TEXT NOT NULL, checks TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT REFERENCES tickets(id),
 operation TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE merges(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL UNIQUE REFERENCES tickets(id),
 commit_sha TEXT NOT NULL, target_branch TEXT NOT NULL, tests TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE questions(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id),
 attempt_id TEXT NOT NULL REFERENCES attempts(id),
 question TEXT NOT NULL, options TEXT NOT NULL, state TEXT NOT NULL, asked_at TEXT NOT NULL);
CREATE UNIQUE INDEX one_open_question ON questions(attempt_id) WHERE state='open';
CREATE TABLE answers(
 id TEXT PRIMARY KEY, question_id TEXT NOT NULL UNIQUE REFERENCES questions(id),
 answer TEXT NOT NULL, answered_by TEXT NOT NULL, answered_by_session_id TEXT, created_at TEXT NOT NULL);
CREATE INDEX questions_ticket ON questions(ticket_id, asked_at);
CREATE INDEX events_ticket ON events(ticket_id, id);
CREATE INDEX tickets_project ON tickets(project_id, created_at);
CREATE TABLE requests(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL);
"""


def build_v6(path, project_root, stamp="2026-01-01T00:00:00+00:00"):
    """写一个内容完整的 v6 库：有问答历史，还没有 notices 与 coordinator_workdir。"""
    prd_sha = hashlib.sha256((Path(project_root) / "PRD.md").read_bytes()).hexdigest()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(V6_SCHEMA)
        conn.execute("INSERT INTO projects VALUES ('p1','项目一',?)", (str(project_root),))
        conn.execute("INSERT INTO tickets VALUES ('t1','p1','还在做','一份清单','PRD.md',?,'[]',1,'running','a1',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO attempts VALUES ('a1','t1',1,'PRD.md',?,'running','executor-1','coordinator-1',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO questions VALUES ('q-1','t1','a1','按旧口径还是新口径？','[]','answered',?)",
                     (stamp,))
        conn.execute("INSERT INTO answers VALUES ('ans-1','q-1','按旧口径','human',NULL,?)", (stamp,))
        conn.execute("INSERT INTO events(ticket_id,operation,detail,created_at) VALUES ('t1','bind_session','{}',?)",
                     (stamp,))
        conn.execute("INSERT INTO requests VALUES ('req-1','fp','{}')")
        conn.execute("PRAGMA user_version=6")
        conn.commit()
    finally:
        conn.close()
    return path


# 冻结的 schema 7：notices 已经有表，但还没有 target_session_id 与 wake_kind。
# 真实库就是这一个形状（三条 notices 都是那时写下的，所以迁移后两列是空串）。
V7_SCHEMA = """
CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, root TEXT NOT NULL UNIQUE);
CREATE TABLE tickets(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), title TEXT NOT NULL,
 deliverable TEXT NOT NULL,
 prd_path TEXT NOT NULL, prd_sha256 TEXT NOT NULL, criteria TEXT NOT NULL,
 revision INTEGER NOT NULL, state TEXT NOT NULL, current_attempt TEXT, created_at TEXT NOT NULL);
CREATE TABLE attempts(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id), revision INTEGER NOT NULL,
 prd_path TEXT NOT NULL, prd_sha256 TEXT NOT NULL,
 state TEXT NOT NULL, session_id TEXT, coordinator_session_id TEXT, coordinator_workdir TEXT,
 created_at TEXT NOT NULL);
CREATE UNIQUE INDEX active_session ON attempts(session_id)
 WHERE session_id IS NOT NULL AND state IN ('blocked','running','submitted');
CREATE TABLE submissions(
 id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id),
 summary TEXT NOT NULL, artifacts TEXT NOT NULL, evidence TEXT NOT NULL, risks TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE reviews(
 id TEXT PRIMARY KEY, submission_id TEXT NOT NULL UNIQUE REFERENCES submissions(id),
 reviewer_session_id TEXT NOT NULL, verdict TEXT NOT NULL, checks TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT REFERENCES tickets(id),
 operation TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE merges(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL UNIQUE REFERENCES tickets(id),
 commit_sha TEXT NOT NULL, target_branch TEXT NOT NULL, tests TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE questions(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id),
 attempt_id TEXT NOT NULL REFERENCES attempts(id),
 question TEXT NOT NULL, options TEXT NOT NULL, state TEXT NOT NULL, asked_at TEXT NOT NULL);
CREATE UNIQUE INDEX one_open_question ON questions(attempt_id) WHERE state='open';
CREATE TABLE notices(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id),
 attempt_id TEXT NOT NULL REFERENCES attempts(id), origin_session_id TEXT NOT NULL,
 status TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX one_sent_notice ON notices(ticket_id) WHERE status='sent';
CREATE INDEX notices_ticket ON notices(ticket_id, created_at);
CREATE TABLE answers(
 id TEXT PRIMARY KEY, question_id TEXT NOT NULL UNIQUE REFERENCES questions(id),
 answer TEXT NOT NULL, answered_by TEXT NOT NULL, answered_by_session_id TEXT, created_at TEXT NOT NULL);
CREATE INDEX questions_ticket ON questions(ticket_id, asked_at);
CREATE INDEX events_ticket ON events(ticket_id, id);
CREATE INDEX tickets_project ON tickets(project_id, created_at);
CREATE TABLE requests(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL);
"""


# 冻结的 schema 8：schema 9 之前的最后一步形状——notices 已经有两列，`submissions`
# 还没有 head_sha，`merges` 还没有 reviewed_sha。真实库（今天）就是这一个形状。
V8_SCHEMA = """
CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, root TEXT NOT NULL UNIQUE);
CREATE TABLE tickets(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), title TEXT NOT NULL,
 deliverable TEXT NOT NULL,
 prd_path TEXT NOT NULL, prd_sha256 TEXT NOT NULL, criteria TEXT NOT NULL,
 revision INTEGER NOT NULL, state TEXT NOT NULL, current_attempt TEXT, created_at TEXT NOT NULL);
CREATE TABLE attempts(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id), revision INTEGER NOT NULL,
 prd_path TEXT NOT NULL, prd_sha256 TEXT NOT NULL,
 state TEXT NOT NULL, session_id TEXT, coordinator_session_id TEXT, coordinator_workdir TEXT,
 created_at TEXT NOT NULL);
CREATE UNIQUE INDEX active_session ON attempts(session_id)
 WHERE session_id IS NOT NULL AND state IN ('blocked','running','submitted');
CREATE TABLE submissions(
 id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id),
 summary TEXT NOT NULL, artifacts TEXT NOT NULL, evidence TEXT NOT NULL, risks TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE reviews(
 id TEXT PRIMARY KEY, submission_id TEXT NOT NULL UNIQUE REFERENCES submissions(id),
 reviewer_session_id TEXT NOT NULL, verdict TEXT NOT NULL, checks TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT REFERENCES tickets(id),
 operation TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE merges(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL UNIQUE REFERENCES tickets(id),
 commit_sha TEXT NOT NULL, target_branch TEXT NOT NULL, tests TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE questions(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id),
 attempt_id TEXT NOT NULL REFERENCES attempts(id),
 question TEXT NOT NULL, options TEXT NOT NULL, state TEXT NOT NULL, asked_at TEXT NOT NULL);
CREATE UNIQUE INDEX one_open_question ON questions(attempt_id) WHERE state='open';
CREATE TABLE notices(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id),
 attempt_id TEXT NOT NULL REFERENCES attempts(id), origin_session_id TEXT NOT NULL,
 status TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL,
 target_session_id TEXT NOT NULL DEFAULT '', wake_kind TEXT NOT NULL DEFAULT '');
CREATE UNIQUE INDEX one_sent_notice ON notices(ticket_id) WHERE status='sent';
CREATE INDEX notices_ticket ON notices(ticket_id, created_at);
CREATE TABLE answers(
 id TEXT PRIMARY KEY, question_id TEXT NOT NULL UNIQUE REFERENCES questions(id),
 answer TEXT NOT NULL, answered_by TEXT NOT NULL, answered_by_session_id TEXT, created_at TEXT NOT NULL);
CREATE INDEX questions_ticket ON questions(ticket_id, asked_at);
CREATE INDEX events_ticket ON events(ticket_id, id);
CREATE INDEX tickets_project ON tickets(project_id, created_at);
CREATE TABLE requests(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL);
"""


def build_v8(path, project_root, stamp="2026-01-01T00:00:00+00:00"):
    """写一个内容完整的 v8 库：一张在跑、一张已验收已合并、一张已验收还没合。

    三张单各对应一个真实现场：还在做的、已经合过的（它那条 merges 行没有 reviewed_sha）、
    已验收等合并的（它那条提交行没有 head_sha）。
    """
    prd_sha = hashlib.sha256((Path(project_root) / "PRD.md").read_bytes()).hexdigest()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(V8_SCHEMA)
        conn.execute("INSERT INTO projects VALUES ('p1','项目一',?)", (str(project_root),))
        conn.execute("INSERT INTO tickets VALUES ('t1','p1','还在做','一份清单','PRD.md',?,'[]',1,'running','a1',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO tickets VALUES ('t2','p1','已经合过','另一份清单','PRD.md',?,'[]',1,'done','a2',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO tickets VALUES ('t3','p1','已验收等合并','第三份清单','PRD.md',?,'[]',1,'done','a3',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO attempts VALUES ('a1','t1',1,'PRD.md',?,'running','executor-1','coordinator-1','D:/协调者',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO attempts VALUES ('a2','t2',1,'PRD.md',?,'accepted','executor-2','coordinator-1','D:/协调者',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO attempts VALUES ('a3','t3',1,'PRD.md',?,'accepted','executor-3','coordinator-1','D:/协调者',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO submissions VALUES ('s1','a2','已交付','[]','[\"自检\"]','[]',?)", (stamp,))
        conn.execute("INSERT INTO submissions VALUES ('s2','a3','已交付','[]','[\"自检\"]','[]',?)", (stamp,))
        conn.execute("INSERT INTO reviews VALUES ('r1','s1','reviewer-1','accept','[]','逐项核对',?)", (stamp,))
        conn.execute("INSERT INTO reviews VALUES ('r2','s2','reviewer-1','accept','[]','逐项核对',?)", (stamp,))
        conn.execute("INSERT INTO merges VALUES ('m1','t2','abc1234','master','300 项全绿',?)", (stamp,))
        conn.execute("INSERT INTO notices VALUES ('n1','t3','a3','origin-1','sent','已发回执',?,'coordinator-1','queued')",
                     (stamp,))
        conn.execute("INSERT INTO events(ticket_id,operation,detail,created_at) VALUES ('t1','bind_session','{}',?)",
                     (stamp,))
        conn.execute("INSERT INTO requests VALUES ('req-1','fp','{}')")
        conn.execute("PRAGMA user_version=8")
        conn.commit()
    finally:
        conn.close()
    return path


# 冻结的 schema 9：`submissions` 已有 head_sha、`merges` 已有 reviewed_sha，
# 还没有 `criteria_version` 与 `base_sha`。正式库（2026-11-21）就是这个形状。
# 写全而不是从 SCHEMA 做减法：升级链每加一级，旧形状都不该跟着变。
V9_SCHEMA = """
CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, root TEXT NOT NULL UNIQUE);
CREATE TABLE tickets(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), title TEXT NOT NULL,
 deliverable TEXT NOT NULL,
 prd_path TEXT NOT NULL, prd_sha256 TEXT NOT NULL, criteria TEXT NOT NULL,
 revision INTEGER NOT NULL, state TEXT NOT NULL, current_attempt TEXT, created_at TEXT NOT NULL);
CREATE TABLE attempts(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id), revision INTEGER NOT NULL,
 prd_path TEXT NOT NULL, prd_sha256 TEXT NOT NULL,
 state TEXT NOT NULL, session_id TEXT, coordinator_session_id TEXT, coordinator_workdir TEXT,
 created_at TEXT NOT NULL);
CREATE UNIQUE INDEX active_session ON attempts(session_id)
 WHERE session_id IS NOT NULL AND state IN ('blocked','running','submitted');
CREATE TABLE submissions(
 id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id),
 summary TEXT NOT NULL, artifacts TEXT NOT NULL, evidence TEXT NOT NULL, risks TEXT NOT NULL,
 created_at TEXT NOT NULL, head_sha TEXT);
CREATE TABLE reviews(
 id TEXT PRIMARY KEY, submission_id TEXT NOT NULL UNIQUE REFERENCES submissions(id),
 reviewer_session_id TEXT NOT NULL, verdict TEXT NOT NULL, checks TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT REFERENCES tickets(id),
 operation TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE merges(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL UNIQUE REFERENCES tickets(id),
 commit_sha TEXT NOT NULL, target_branch TEXT NOT NULL, tests TEXT NOT NULL, created_at TEXT NOT NULL,
 reviewed_sha TEXT);
CREATE TABLE questions(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id),
 attempt_id TEXT NOT NULL REFERENCES attempts(id),
 question TEXT NOT NULL, options TEXT NOT NULL, state TEXT NOT NULL, asked_at TEXT NOT NULL);
CREATE UNIQUE INDEX one_open_question ON questions(attempt_id) WHERE state='open';
CREATE TABLE notices(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id),
 attempt_id TEXT NOT NULL REFERENCES attempts(id), origin_session_id TEXT NOT NULL,
 status TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL,
 target_session_id TEXT NOT NULL DEFAULT '', wake_kind TEXT NOT NULL DEFAULT '');
CREATE UNIQUE INDEX one_sent_notice ON notices(ticket_id) WHERE status='sent';
CREATE INDEX notices_ticket ON notices(ticket_id, created_at);
CREATE TABLE answers(
 id TEXT PRIMARY KEY, question_id TEXT NOT NULL UNIQUE REFERENCES questions(id),
 answer TEXT NOT NULL, answered_by TEXT NOT NULL, answered_by_session_id TEXT, created_at TEXT NOT NULL);
CREATE INDEX questions_ticket ON questions(ticket_id, asked_at);
CREATE INDEX events_ticket ON events(ticket_id, id);
CREATE INDEX tickets_project ON tickets(project_id, created_at);
CREATE TABLE requests(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL);
"""


def build_v9(path, project_root, stamp="2026-01-01T00:00:00+00:00"):
    """写一个内容完整的 v9 库：一张在跑、一张已验收、一条带 sha 的提交与一条老提交。

    故意造两种提交：一条有 `head_sha`（v9 才有的列），一条没有——迁移给
    `base_sha` 补值时，正是这两种行要一起留空。
    """
    prd_sha = hashlib.sha256((Path(project_root) / "PRD.md").read_bytes()).hexdigest()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(V9_SCHEMA)
        conn.execute("INSERT INTO projects VALUES ('p1','项目一',?)", (str(project_root),))
        conn.execute("INSERT INTO tickets VALUES ('t1','p1','还在做','一份清单','PRD.md',?,'[]',1,'running','a1',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO tickets VALUES ('t2','p1','已验收','另一份清单','PRD.md',?,'[]',1,'done','a2',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO attempts VALUES ('a1','t1',1,'PRD.md',?,'running','executor-1','coordinator-1','D:/协调者',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO attempts VALUES ('a2','t2',1,'PRD.md',?,'accepted','executor-2','coordinator-1','D:/协调者',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO submissions VALUES ('s1','a2','已交付','[]','[\"自检\"]','[]',?,?)",
                     (stamp, "0123456789abcdef0123456789abcdef01234567"))
        conn.execute("INSERT INTO reviews VALUES ('r1','s1','reviewer-1','accept','[]','逐项核对',?)", (stamp,))
        conn.execute("INSERT INTO merges VALUES ('m1','t2','abc1234','master','300 项全绿',?,NULL)", (stamp,))
        conn.execute("INSERT INTO events(ticket_id,operation,detail,created_at) VALUES ('t1','bind_session','{}',?)",
                     (stamp,))
        conn.execute("INSERT INTO requests VALUES ('req-1','fp','{}')")
        conn.execute("PRAGMA user_version=9")
        conn.commit()
    finally:
        conn.close()
    return path



def build_v10(path, project_root, stamp="2026-01-01T00:00:00+00:00"):
    """写一个内容完整的 v10 库：正式库 2026-11-21 迁移前的形状。

    在 v9 形状上补上 9 -> 10 那三列（criteria_version ×2、base_sha），再把
    `tickets.prd_sha256` 留着 NOT NULL —— 10 -> 11 要放宽的正是这一位。
    工单号故意用 `AW-095` / `AW-096`：发号盘的初始化要按已有最大号算出下一个 97。
    """
    path = build_v9(path, project_root, stamp)
    conn = sqlite3.connect(path)
    try:
        conn.execute("ALTER TABLE tickets ADD COLUMN criteria_version INTEGER NOT NULL DEFAULT 1")
        conn.execute("ALTER TABLE attempts ADD COLUMN criteria_version INTEGER NOT NULL DEFAULT 1")
        conn.execute("ALTER TABLE submissions ADD COLUMN base_sha TEXT")
        conn.execute("DELETE FROM attempts")
        conn.execute("DELETE FROM submissions")
        conn.execute("DELETE FROM reviews")
        conn.execute("DELETE FROM merges")
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM requests")
        conn.execute("DELETE FROM tickets")
        conn.execute("DELETE FROM projects")
        prd_sha = hashlib.sha256((Path(project_root) / "PRD.md").read_bytes()).hexdigest()
        conn.execute("INSERT INTO projects VALUES ('ai-workspace','工作区',?)", (str(project_root),))
        conn.execute("INSERT INTO tickets(id,project_id,title,deliverable,prd_path,prd_sha256,criteria,revision,"
                     "criteria_version,state,current_attempt,created_at)"
                     " VALUES ('AW-095','ai-workspace','已经验收','一份清单','PRD.md',?,'[]',1,1,'done','a1',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO tickets(id,project_id,title,deliverable,prd_path,prd_sha256,criteria,revision,"
                     "criteria_version,state,current_attempt,created_at)"
                     " VALUES ('AW-096','ai-workspace','还在做','另一份清单','PRD.md',?,'[]',1,1,'running','a2',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO attempts(id,ticket_id,revision,criteria_version,prd_path,prd_sha256,state,"
                     "session_id,coordinator_session_id,coordinator_workdir,created_at)"
                     " VALUES ('a1','AW-095',1,1,'PRD.md',?,'accepted','executor-1','coordinator-1','D:/协调者',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO attempts(id,ticket_id,revision,criteria_version,prd_path,prd_sha256,state,"
                     "session_id,coordinator_session_id,coordinator_workdir,created_at)"
                     " VALUES ('a2','AW-096',1,1,'PRD.md',?,'running','executor-2','coordinator-1','D:/协调者',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO submissions VALUES ('s1','a1','已交付','[]','[\"自检\"]','[]',?,?,NULL)",
                     (stamp, "0123456789abcdef0123456789abcdef01234567"))
        conn.execute("INSERT INTO reviews VALUES ('r1','s1','reviewer-1','accept','[]','逐项核对',?)", (stamp,))
        conn.execute("INSERT INTO merges VALUES ('m1','AW-095','abc1234','master','300 项全绿',?,NULL)", (stamp,))
        conn.execute("INSERT INTO events(ticket_id,operation,detail,created_at) VALUES ('AW-096','bind_session','{}',?)",
                     (stamp,))
        conn.execute("INSERT INTO requests VALUES ('req-1','fp','{}')")
        conn.execute("PRAGMA user_version=10")
        conn.commit()
    finally:
        conn.close()
    return path


# 「声称 v10 却已经有 v11 的指纹放宽」的现场：把 tickets 表按无 NOT NULL 的形状重建。
# 只用于形状守卫那一条用例——列集合没变，变的正是 PRAGMA table_info 里的 notnull 位。
_NULLABLE_TICKETS_REBUILD = """
CREATE TABLE tickets_nullable(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), title TEXT NOT NULL,
 deliverable TEXT NOT NULL, prd_path TEXT NOT NULL, prd_sha256 TEXT, criteria TEXT NOT NULL,
 revision INTEGER NOT NULL, criteria_version INTEGER NOT NULL DEFAULT 1,
 state TEXT NOT NULL, current_attempt TEXT, created_at TEXT NOT NULL);
INSERT INTO tickets_nullable SELECT id,project_id,title,deliverable,prd_path,prd_sha256,criteria,revision,
 criteria_version,state,current_attempt,created_at FROM tickets;
DROP TABLE tickets;
ALTER TABLE tickets_nullable RENAME TO tickets;
"""


V11_TABLES = TABLES + ("merges", "questions", "answers", "notices", "ticket_links")
# v12 多出车次表；v13 只给 submissions 加一列、不改表清单，所以 v13 与 v12 的表清单逐字相同。
V12_TABLES = V11_TABLES + ("batches",)


def build_v11(path, project_root, stamp="2026-01-01T00:00:00+00:00"):
    """写一个内容完整的 v11 库：正式库 2026-11-21 升 schema 12 之前的形状。

    在 v10 形状上补上 10 -> 11 那三样（发号盘 `next_number`、可空的 `tickets.prd_sha256`、
    `ticket_links` 与反向索引），还没有 `batches` 表、`tickets.batch_id` 与 `attempts.batch_key`
    ——12 要加的正是这三样，且一个值都不回溯。
    """
    path = build_v10(path, project_root, stamp)
    conn = sqlite3.connect(path)
    try:
        # 真实登记走 register_project，库里存的是规范化后的绝对路径；这里对齐它，
        # 否则临时目录的 8.3 短名会让 bind_session 的目录核对对不上。
        conn.execute("UPDATE projects SET root=? WHERE id='ai-workspace'",
                     (os.path.normcase(str(Path(project_root).resolve())),))
        conn.execute("ALTER TABLE projects ADD COLUMN next_number INTEGER NOT NULL DEFAULT 97")
        conn.executescript(_NULLABLE_TICKETS_REBUILD)
        conn.execute("CREATE TABLE ticket_links("
                     " from_ticket TEXT NOT NULL REFERENCES tickets(id), kind TEXT NOT NULL,"
                     " to_ticket TEXT NOT NULL REFERENCES tickets(id), created_at TEXT NOT NULL,"
                     " PRIMARY KEY (from_ticket, kind, to_ticket),"
                     " CHECK (kind IN ('blocked_by','related','rides_with')))")
        conn.execute("CREATE INDEX ticket_links_to ON ticket_links(to_ticket, kind)")
        conn.execute("PRAGMA user_version=11")
        conn.commit()
    finally:
        conn.close()
    return path


def build_v12(path, project_root, stamp="2026-01-01T00:00:00+00:00"):
    """写一个内容完整的 v12 库：正式库升 schema 13 之前的形状。

    在 v11 形状上补上 11 -> 12 那三样（`batches` 表、`tickets.batch_id`、`attempts.batch_key`，
    并把 `active_session` 收窄成只守无车次单），还没有 `submissions.conflict_preview`
    ——13 要加的正是那一列，且存量提交一个值都不回溯。
    """
    path = build_v11(path, project_root, stamp)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE batches("
                     " id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),"
                     " resource_key TEXT NOT NULL, owner_session_id TEXT,"
                     " planned_at TEXT NOT NULL, closed_at TEXT)")
        conn.execute("ALTER TABLE tickets ADD COLUMN batch_id TEXT REFERENCES batches(id)")
        conn.execute("ALTER TABLE attempts ADD COLUMN batch_key TEXT")
        conn.execute("DROP INDEX IF EXISTS active_session")
        conn.execute("CREATE UNIQUE INDEX active_session ON attempts(session_id)"
                     " WHERE batch_key IS NULL AND session_id IS NOT NULL"
                     " AND state IN ('blocked','running','submitted')")
        conn.execute("PRAGMA user_version=12")
        conn.commit()
    finally:
        conn.close()
    return path


def build_v13(path, project_root, stamp="2026-01-01T00:00:00+00:00"):
    """写一个内容完整的 v13 库：正式库升 schema 14 之前的形状。

    在 v12 形状上补上 12 -> 13 的那一列（`submissions.conflict_preview`）。这个夹具三列都缺：
    `tickets.requester_session_id` 与 `notices.message_id`（14 加的），以及 `tickets.owner_session_id`
    （15 加的）。三张单分别覆盖 C5 的三种情形：
    `AW-095` 已验收但当前尝试还在、`AW-096` 还在跑、`AW-097` 没有当前尝试（owner 必须留 NULL）。
    """
    path = build_v12(path, project_root, stamp)
    conn = sqlite3.connect(path)
    try:
        conn.execute("ALTER TABLE submissions ADD COLUMN conflict_preview TEXT")
        conn.execute("INSERT INTO tickets(id,project_id,title,deliverable,prd_path,prd_sha256,criteria,revision,"
                     "criteria_version,state,current_attempt,created_at,batch_id)"
                     " VALUES ('AW-097','ai-workspace','排队中','第三份清单','PRD.md',NULL,'[]',1,1,'planned',NULL,?,NULL)",
                     (stamp,))
        conn.execute("PRAGMA user_version=13")
        conn.commit()
    finally:
        conn.close()
    return path


def build_v14(path, project_root, stamp="2026-01-01T00:00:00+00:00"):
    """写一个内容完整的 v14 库：正式库升 schema 15 之前的形状。

    在 v13 形状上只补 13 -> 14 那两列（`tickets.requester_session_id`、`notices.message_id`），
    `tickets.owner_session_id` 仍缺——那是 15 的唯一任务。有了这一级，“声称 14 却已经有
    owner” 这种旧版本头配新形状才有一条能跑的用例。
    """
    path = build_v13(path, project_root, stamp)
    conn = sqlite3.connect(path)
    try:
        conn.execute("ALTER TABLE tickets ADD COLUMN requester_session_id TEXT")
        conn.execute("ALTER TABLE notices ADD COLUMN message_id TEXT")
        conn.execute("PRAGMA user_version=14")
        conn.commit()
    finally:
        conn.close()
    return path


def build_v15(path, project_root, stamp="2026-01-01T00:00:00+00:00"):
    """写一个内容完整的 v15 库：正式库升 schema 16 之前的形状。

    在 v13 形状上补 13 -> 14（`tickets.requester_session_id`、`notices.message_id`）与
    14 -> 15（`tickets.owner_session_id`，按当前尝试回填）两级的产物，`merges` 仍是
    七列的老形状——16 要加的正是 `source` / `verified_by` / `verified_at` / `note`，
    且存量合并行一个值都不回溯（`m1` 升级后这四列必须全是 NULL）。
    """
    path = build_v13(path, project_root, stamp)
    conn = sqlite3.connect(path)
    try:
        conn.execute("ALTER TABLE tickets ADD COLUMN requester_session_id TEXT")
        conn.execute("ALTER TABLE notices ADD COLUMN message_id TEXT")
        conn.execute("ALTER TABLE tickets ADD COLUMN owner_session_id TEXT")
        conn.execute("UPDATE tickets SET owner_session_id = ("
                     " SELECT a.coordinator_session_id FROM attempts a WHERE a.id = tickets.current_attempt)"
                     " WHERE owner_session_id IS NULL AND current_attempt IS NOT NULL")
        conn.execute("PRAGMA user_version=15")
        conn.commit()
    finally:
        conn.close()
    return path


def build_v7(path, project_root, stamp="2026-01-01T00:00:00+00:00"):
    """写一个内容完整的 v7 库：两张工单（running / done）与三条存量 notices。"""
    prd_sha = hashlib.sha256((Path(project_root) / "PRD.md").read_bytes()).hexdigest()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(V7_SCHEMA)
        conn.execute("INSERT INTO projects VALUES ('p1','项目一',?)", (str(project_root),))
        conn.execute("INSERT INTO tickets VALUES ('t1','p1','还在做','一份清单','PRD.md',?,'[]',1,'running','a1',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO tickets VALUES ('t2','p1','已经验收','另一份清单','PRD.md',?,'[]',1,'done','a2',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO attempts VALUES ('a1','t1',1,'PRD.md',?,'running','executor-1','coordinator-1','D:/协调者',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO attempts VALUES ('a2','t2',1,'PRD.md',?,'accepted','executor-2','coordinator-1','D:/协调者',?)",
                     (prd_sha, stamp))
        conn.execute("INSERT INTO submissions VALUES ('s1','a2','已交付','[]','[\"自检\"]','[]',?)", (stamp,))
        conn.execute("INSERT INTO reviews VALUES ('r1','s1','reviewer-1','accept','[]','逐项核对',?)", (stamp,))
        conn.execute("INSERT INTO notices VALUES ('n1','t1','a1','origin-1','sent','已发回执',?)", (stamp,))
        conn.execute("INSERT INTO notices VALUES ('n2','t2','a2','origin-1','failed','目标会话已归档',?)", (stamp,))
        conn.execute("INSERT INTO notices VALUES ('n3','t2','a2','origin-1','failed','重试仍失败',?)", (stamp,))
        conn.execute("INSERT INTO events(ticket_id,operation,detail,created_at) VALUES ('t1','bind_session','{}',?)",
                     (stamp,))
        conn.execute("INSERT INTO requests VALUES ('req-1','fp','{}')")
        conn.execute("PRAGMA user_version=7")
        conn.commit()
    finally:
        conn.close()
    return path


def notice_rows(path):
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM notices ORDER BY rowid")]
    finally:
        conn.close()


def index_sql(path, name):
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def table_names(path):
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def columns(path, table):
    conn = sqlite3.connect(path)
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def user_version(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


class MigrateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "项目一"
        self.project.mkdir()
        (self.project / "PRD.md").write_text("需求第一版", encoding="utf-8")
        self.db = build_v3(self.root / "center-v3.sqlite3", self.project)

    def fresh_db(self):
        path = self.root / "center-current.sqlite3"
        result = Center(path).execute("init", {})
        self.assertTrue(result["ok"], result)
        return path

    def test_upgrade_keeps_every_row_and_adds_the_column(self):
        before = counts(self.db)
        result = Center(self.db).execute("migrate", {})
        self.assertTrue(result["ok"], result)
        receipt = result["result"]
        self.assertEqual(3, receipt["from_version"])
        self.assertEqual(SCHEMA_VERSION, receipt["schema_version"])
        self.assertTrue(receipt["migrated"])
        self.assertEqual(SCHEMA_VERSION, user_version(self.db))
        self.assertIn("coordinator_session_id", columns(self.db, "attempts"))
        self.assertIn("merges", table_names(self.db))
        self.assertEqual(before, counts(self.db))
        # 升级后原来的工单仍能通过工具读到，历史字段一个不少。
        ticket = Center(self.db).execute("get_ticket", {"project_id": "p1", "ticket_id": "t1"})
        self.assertTrue(ticket["ok"], ticket)
        self.assertEqual("running", ticket["result"]["state"])
        self.assertEqual("executor-1", ticket["result"]["attempts"][0]["session_id"])
        self.assertIsNone(ticket["result"]["attempts"][0]["coordinator_session_id"])
        self.assertEqual(["accept"], [item["verdict"] for item in ticket["result"]["reviews"]])
        # 升级后的库能正常走新字段：claim 写进 attempts，别的会话才绑得上。
        # 这里的 t1 还在 running，本用例验的是升级后新字段可写，显式越过串行（AW-061）。
        claim = Center(self.db).execute("claim_ticket", {
            "project_id": "p1", "ticket_id": "t2", "expected_revision": 1,
            "coordinator_session_id": "coordinator-1", "allow_parallel": True,
            "parallel_reason": "验的是升级后新字段可写，不是项目串行",
        }, "claim-after-migrate")
        self.assertTrue(claim["ok"], claim)
        after = Center(self.db).execute("get_ticket", {"project_id": "p1", "ticket_id": "t2"})
        self.assertEqual("coordinator-1", after["result"]["attempts"][0]["coordinator_session_id"])
        self.assertIsNone(after["result"]["attempts"][0]["session_id"])

    def test_upgrade_on_a_fresh_database_reports_it_is_current(self):
        db = self.fresh_db()
        before = counts(db)
        result = Center(db).execute("migrate", {})
        self.assertTrue(result["ok"], result)
        self.assertEqual(SCHEMA_VERSION, result["result"]["schema_version"])
        self.assertFalse(result["result"]["migrated"])
        self.assertIn("已是最新", result["result"]["message"])
        self.assertEqual(before, counts(db))
        # 再跑一次仍然是「已是最新」，不是升级。
        again = Center(db).execute("migrate", {})
        self.assertTrue(again["ok"], again)
        self.assertFalse(again["result"]["migrated"])

    def test_upgrade_is_idempotent_on_a_migrated_database(self):
        self.assertTrue(Center(self.db).execute("migrate", {})["ok"])
        before = counts(self.db)
        second = Center(self.db).execute("migrate", {})
        self.assertTrue(second["ok"], second)
        self.assertFalse(second["result"]["migrated"])
        self.assertFalse(second["result"]["migrated"])
        self.assertEqual(SCHEMA_VERSION, user_version(self.db))
        self.assertEqual(before, counts(self.db))

    # --- 5 -> 6：问答两张表与 blocked 索引谓词 ------------------------------

    def test_upgrade_from_v5_adds_question_tables_and_widens_the_index(self):
        legacy = build_v5(self.root / "center-v5.sqlite3", self.project)
        before = counts(legacy)
        result = Center(legacy).execute("migrate", {})
        self.assertTrue(result["ok"], result)
        receipt = result["result"]
        self.assertEqual(5, receipt["from_version"])
        self.assertEqual(SCHEMA_VERSION, receipt["schema_version"])
        self.assertTrue(receipt["migrated"])
        self.assertEqual(SCHEMA_VERSION, user_version(legacy))
        self.assertLessEqual({"questions", "answers"}, table_names(legacy))
        index = index_sql(legacy, "active_session")
        self.assertIn("'blocked'", index)
        self.assertIn("'running'", index)
        self.assertIn("'submitted'", index)
        self.assertEqual(before, counts(legacy))
        center = Center(legacy)
        # 旧工单、尝试、合并记录一行没少，且仍能读出来。
        listed = center.execute("list_tickets", {})
        self.assertTrue(listed["ok"], listed)
        self.assertEqual(["t1", "t2"], [item["id"] for item in listed["result"]["items"]])
        ticket = center.execute("get_ticket", {"project_id": "p1", "ticket_id": "t1"})
        self.assertTrue(ticket["ok"], ticket)
        self.assertEqual("running", ticket["result"]["state"])
        self.assertEqual("executor-1", ticket["result"]["attempts"][0]["session_id"])
        self.assertEqual([], ticket["result"]["questions"])
        self.assertEqual([], ticket["result"]["answers"])
        delivered = center.execute("get_ticket", {"project_id": "p1", "ticket_id": "t2"})
        self.assertEqual(["m1"], [item["id"] for item in delivered["result"]["merges"]])
        # 升级后的库能直接用新能力：在旧工单上提问，两处同时进 blocked。
        asked = center.execute("ask_ticket", {
            "project_id": "p1", "ticket_id": "t1", "attempt_id": "a1", "session_id": "executor-1",
            "question": "按旧口径还是新口径？", "options": [],
        }, "ask-after-migrate")
        self.assertTrue(asked["ok"], asked)
        blocked = center.execute("get_ticket", {"project_id": "p1", "ticket_id": "t1"})["result"]
        self.assertEqual("blocked", blocked["state"])
        self.assertEqual("blocked", blocked["attempts"][0]["state"])
        self.assertEqual(1, len(blocked["questions"]))

    # --- 7 -> 8：回执目标与会话唤醒方式 ------------------------------------

    def test_upgrade_from_v7_adds_notice_target_and_wake_kind(self):
        legacy = build_v7(self.root / "center-v7.sqlite3", self.project)
        before = counts(legacy)
        old_notices = notice_rows(legacy)
        result = Center(legacy).execute("migrate", {})
        self.assertTrue(result["ok"], result)
        receipt = result["result"]
        self.assertEqual(7, receipt["from_version"])
        self.assertEqual(SCHEMA_VERSION, receipt["schema_version"])
        self.assertTrue(receipt["migrated"])
        self.assertEqual(SCHEMA_VERSION, user_version(legacy))
        self.assertIn("target_session_id", columns(legacy, "notices"))
        self.assertIn("wake_kind", columns(legacy, "notices"))
        self.assertEqual(before, counts(legacy))
        # 存量三条一条不少，两列留空串表示「那时还没有这两项」，不补一个猜的值。
        after = notice_rows(legacy)
        self.assertEqual([row["id"] for row in old_notices], [row["id"] for row in after])
        for row in after:
            self.assertEqual("", row["target_session_id"])
            self.assertEqual("", row["wake_kind"])
        center = Center(legacy)
        listed = center.execute("list_tickets", {})
        self.assertTrue(listed["ok"], listed)
        self.assertEqual(["t1", "t2"], [item["id"] for item in listed["result"]["items"]])
        ticket = center.execute("get_ticket", {"project_id": "p1", "ticket_id": "t2"})
        self.assertTrue(ticket["ok"], ticket)
        self.assertEqual(["n2", "n3"], [item["id"] for item in ticket["result"]["notices"]])
        self.assertEqual(["accept"], [item["verdict"] for item in ticket["result"]["reviews"]])
        # 升级后新字段可用：协调会话记一条带投递目标与 wake_kind 的回执（t1 已有条 sent，这里记 failed）。
        noticed = center.execute("record_notice", {
            "project_id": "p1", "ticket_id": "t1", "attempt_id": "a1",
            "session_id": "coordinator-1", "origin_session_id": "origin-1",
            "status": "failed", "detail": "目标会话已归档，发送失败",
            "target_session_id": "coordinator-1", "wake_kind": "resumed",
        }, "notice-after-v8")
        self.assertTrue(noticed["ok"], noticed)
        read = center.execute("get_ticket", {"project_id": "p1", "ticket_id": "t1"})["result"]
        self.assertEqual("failed", read["notices"][-1]["status"])
        self.assertEqual("resumed", read["notices"][-1]["wake_kind"])
        self.assertEqual("coordinator-1", read["notices"][-1]["target_session_id"])

    def test_upgrade_from_v7_keeps_merge_gate_and_accepts_a_notice(self):
        legacy = build_v7(self.root / "center-v7-merge.sqlite3", self.project)
        self.assertTrue(Center(legacy).execute("migrate", {})["ok"])
        center = Center(legacy)
        # reviewed_sha 传 unverified：v7 库里没有 head_sha 这一列，这条提交是历史提交。
        payload = {"project_id": "p1", "ticket_id": "t1", "commit": "a1b2c3d",
                   "target_branch": "master", "tests": "269 项全绿", "reviewed_sha": "unverified"}
        # t1 有一条 sent：合并门放行（状态还是 running，所以先撞 INVALID_STATE）。
        self.assertEqual("INVALID_STATE", center.execute("merge_ticket", payload, "merge-running")["error"]["code"])
        # t2 已 done 且有两条 failed：failed 也算走过回执，能写合并记录。
        merged = center.execute("merge_ticket", {**payload, "ticket_id": "t2"}, "merge-after-v8")
        self.assertTrue(merged["ok"], merged)
        self.assertEqual("t2", merged["result"]["ticket_id"])

    # --- 8 -> 9：被验收的树与要合的树 ---------------------------------------

    def test_upgrade_from_v8_adds_head_sha_and_reviewed_sha(self):
        legacy = build_v8(self.root / "center-v8.sqlite3", self.project)
        before = counts(legacy, TABLES + ("merges",))
        result = Center(legacy).execute("migrate", {})
        self.assertTrue(result["ok"], result)
        receipt = result["result"]
        self.assertEqual(8, receipt["from_version"])
        self.assertEqual(SCHEMA_VERSION, receipt["schema_version"])
        self.assertTrue(receipt["migrated"])
        self.assertEqual(SCHEMA_VERSION, user_version(legacy))
        self.assertIn("head_sha", columns(legacy, "submissions"))
        self.assertIn("reviewed_sha", columns(legacy, "merges"))
        # 旧数据一行不丢：两张提交、一条合并记录、一条回执与事件全在。
        self.assertEqual(before, counts(legacy, TABLES + ("merges",)))
        conn = sqlite3.connect(legacy)
        try:
            self.assertEqual([None, None], [row[0] for row in conn.execute(
                "SELECT head_sha FROM submissions ORDER BY rowid")])
            self.assertEqual([None], [row[0] for row in conn.execute(
                "SELECT reviewed_sha FROM merges ORDER BY rowid")])
        finally:
            conn.close()
        center = Center(legacy)
        # 升级后能读：旧单的提交与合并记录都读得出来，两个新列是空值而不是空串。
        delivered = center.execute("get_ticket", {"project_id": "p1", "ticket_id": "t2"})["result"]
        self.assertEqual("abc1234", delivered["merges"][0]["commit_sha"])
        self.assertIsNone(delivered["merges"][0]["reviewed_sha"])
        self.assertIsNone(delivered["submissions"][0]["head_sha"])
        # 老单合并：必须显式传字面量 unverified，传了就把这句话原样入库。
        payload = {"project_id": "p1", "ticket_id": "t3", "commit": "def5678",
                   "target_branch": "master", "tests": "300 项全绿"}
        self.assertEqual("INVALID_INPUT", center.execute(
            "merge_ticket", {**payload, "reviewed_sha": "0123456789abcdef0123456789abcdef01234567"},
            "merge-legacy-bad")["error"]["code"])
        merged = center.execute("merge_ticket", {**payload, "reviewed_sha": "unverified"},
                                "merge-legacy-ok")
        self.assertTrue(merged["ok"], merged)
        after = center.execute("get_ticket", {"project_id": "p1", "ticket_id": "t3"})["result"]
        self.assertEqual("unverified", after["merges"][0]["reviewed_sha"])

    def test_v8_header_with_head_sha_is_rejected_untouched(self):
        legacy = build_v8(self.root / "center-v8-claims-new.sqlite3", self.project)
        conn = sqlite3.connect(legacy)
        conn.execute("ALTER TABLE submissions ADD COLUMN head_sha TEXT")
        conn.commit()
        conn.close()
        before = digest(legacy)
        result = Center(legacy).execute("migrate", {})
        self.assertFalse(result["ok"], result)
        self.assertEqual("SCHEMA_MISMATCH", result["error"]["code"])
        # 拒绝时版本号与文件字节都不许变：它还是 v8，不该被顺手升成 9。
        self.assertEqual(8, user_version(legacy))
        self.assertEqual(before, digest(legacy))

    # --- 9 -> 10：判据版本与基座身份 ---------------------------------------

    def test_upgrade_from_v9_adds_criteria_version_and_base_sha(self):
        legacy = build_v9(self.root / "center-v9.sqlite3", self.project)
        before = counts(legacy, TABLES + ("merges",))
        result = Center(legacy).execute("migrate", {})
        self.assertTrue(result["ok"], result)
        receipt = result["result"]
        self.assertEqual(9, receipt["from_version"])
        self.assertEqual(SCHEMA_VERSION, receipt["schema_version"])
        self.assertTrue(receipt["migrated"])
        self.assertEqual(SCHEMA_VERSION, user_version(legacy))
        self.assertIn("criteria_version", columns(legacy, "tickets"))
        self.assertIn("criteria_version", columns(legacy, "attempts"))
        self.assertIn("base_sha", columns(legacy, "submissions"))
        # 行数一行不少：两张单、两条尝试、一条提交、一条验收与一条合并记录。
        self.assertEqual(before, counts(legacy, TABLES + ("merges",)))
        conn = sqlite3.connect(legacy)
        try:
            # 存量行不编值：判据版本一律填 1（不是 0），基座 sha 一律留 NULL
            # （老提交没有这一项，补一个猜测值就把推定写成了事实）。
            self.assertEqual([1, 1], [row[0] for row in conn.execute(
                "SELECT criteria_version FROM tickets ORDER BY rowid")])
            self.assertEqual([1, 1], [row[0] for row in conn.execute(
                "SELECT criteria_version FROM attempts ORDER BY rowid")])
            self.assertEqual([None], [row[0] for row in conn.execute(
                "SELECT base_sha FROM submissions ORDER BY rowid")])
        finally:
            conn.close()
        # 升级后的库能正常读写这两个新字段。
        center = Center(legacy)
        detail = center.execute("get_ticket", {"project_id": "p1", "ticket_id": "t2"})["result"]
        self.assertEqual(1, detail["criteria_version"])
        self.assertEqual(1, detail["attempts"][0]["criteria_version"])
        self.assertIsNone(detail["submissions"][0]["base_sha"])
        self.assertEqual("0123456789abcdef0123456789abcdef01234567",
                         detail["submissions"][0]["head_sha"])
        listed = center.execute("list_tickets", {})["result"]
        self.assertIn("criteria_v", listed["columns"])
        self.assertIn("behind", listed["columns"])

    def test_second_migration_on_v9_upgrade_reports_current(self):
        legacy = build_v9(self.root / "center-v9-again.sqlite3", self.project)
        self.assertTrue(Center(legacy).execute("migrate", {})["ok"])
        before = counts(legacy, TABLES + ("merges",))
        again = Center(legacy).execute("migrate", {})
        self.assertTrue(again["ok"], again)
        self.assertFalse(again["result"]["migrated"])
        self.assertIn("已是最新", again["result"]["message"])
        self.assertEqual(SCHEMA_VERSION, user_version(legacy))
        self.assertEqual(before, counts(legacy, TABLES + ("merges",)))

    def test_v9_header_with_new_columns_is_rejected_untouched(self):
        legacy = build_v9(self.root / "center-v9-claims-new.sqlite3", self.project)
        conn = sqlite3.connect(legacy)
        conn.execute("ALTER TABLE tickets ADD COLUMN criteria_version INTEGER NOT NULL DEFAULT 1")
        conn.commit()
        conn.close()
        before = digest(legacy)
        result = Center(legacy).execute("migrate", {})
        self.assertFalse(result["ok"], result)
        self.assertEqual("SCHEMA_MISMATCH", result["error"]["code"])
        self.assertEqual(9, user_version(legacy))
        self.assertEqual(before, digest(legacy))

    def test_v7_header_with_new_notice_columns_is_rejected_untouched(self):
        legacy = build_v7(self.root / "center-v7-claims-new.sqlite3", self.project)
        conn = sqlite3.connect(legacy)
        conn.execute("ALTER TABLE notices ADD COLUMN wake_kind TEXT NOT NULL DEFAULT ''")
        conn.commit()
        conn.close()
        before = digest(legacy)
        result = Center(legacy).execute("migrate", {})
        self.assertFalse(result["ok"], result)
        self.assertEqual("SCHEMA_MISMATCH", result["error"]["code"])
        self.assertEqual(7, user_version(legacy))
        self.assertEqual(before, digest(legacy))

    def test_v5_header_with_question_tables_is_rejected_untouched(self):
        legacy = build_v5(self.root / "center-v5-claims-old.sqlite3", self.project)
        conn = sqlite3.connect(legacy)
        conn.execute("CREATE TABLE questions(id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
        before = digest(legacy)
        result = Center(legacy).execute("migrate", {})
        self.assertFalse(result["ok"], result)
        self.assertEqual("SCHEMA_MISMATCH", result["error"]["code"])
        self.assertEqual(5, user_version(legacy))
        self.assertEqual(before, digest(legacy))


    # --- 6 -> 7：协调者工作目录与回信记录 ----------------------------------

    def test_upgrade_from_v6_adds_notices_and_coordinator_workdir(self):
        legacy = build_v6(self.root / "center-v6.sqlite3", self.project)
        before = counts(legacy)
        result = Center(legacy).execute("migrate", {})
        self.assertTrue(result["ok"], result)
        receipt = result["result"]
        self.assertEqual(6, receipt["from_version"])
        self.assertEqual(SCHEMA_VERSION, receipt["schema_version"])
        self.assertTrue(receipt["migrated"])
        self.assertEqual(SCHEMA_VERSION, user_version(legacy))
        self.assertIn("coordinator_workdir", columns(legacy, "attempts"))
        self.assertIn("notices", table_names(legacy))
        self.assertIn("status='sent'", index_sql(legacy, "one_sent_notice"))
        self.assertEqual(before, counts(legacy))
        center = Center(legacy)
        # 旧工单、问答一行没少，且仍能读出来；notices 初始为空数组。
        ticket = center.execute("get_ticket", {"project_id": "p1", "ticket_id": "t1"})
        self.assertTrue(ticket["ok"], ticket)
        self.assertEqual("running", ticket["result"]["state"])
        self.assertEqual("executor-1", ticket["result"]["attempts"][0]["session_id"])
        self.assertIsNone(ticket["result"]["attempts"][0]["coordinator_workdir"])
        self.assertEqual(["q-1"], [item["id"] for item in ticket["result"]["questions"]])
        self.assertEqual(["ans-1"], [item["id"] for item in ticket["result"]["answers"]])
        self.assertEqual([], ticket["result"]["notices"])
        # 升级后的库能直接用新能力：协调会话记一条回信，get_ticket 读得到。
        noticed = center.execute("record_notice", {
            "project_id": "p1", "ticket_id": "t1", "attempt_id": "a1",
            "session_id": "coordinator-1", "origin_session_id": "origin-1",
            "status": "sent", "detail": "已发回执",
            "target_session_id": "coordinator-1", "wake_kind": "queued",
        }, "notice-after-migrate")
        self.assertTrue(noticed["ok"], noticed)
        after = center.execute("get_ticket", {"project_id": "p1", "ticket_id": "t1"})["result"]
        self.assertEqual(["sent"], [item["status"] for item in after["notices"]])
        self.assertEqual("origin-1", after["notices"][0]["origin_session_id"])

    def test_v6_header_with_notices_is_rejected_untouched(self):
        legacy = build_v6(self.root / "center-v6-claims-new.sqlite3", self.project)
        conn = sqlite3.connect(legacy)
        conn.execute("CREATE TABLE notices(id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
        before = digest(legacy)
        result = Center(legacy).execute("migrate", {})
        self.assertFalse(result["ok"], result)
        self.assertEqual("SCHEMA_MISMATCH", result["error"]["code"])
        self.assertEqual(6, user_version(legacy))
        self.assertEqual(before, digest(legacy))

    def test_unknown_version_is_rejected_without_touching_the_file(self):
        for version in (2, 99):
            with self.subTest(version=version):
                path = self.root / f"center-v{version}.sqlite3"
                shutil.copyfile(self.db, path)
                conn = sqlite3.connect(path)
                conn.execute(f"PRAGMA user_version={version}")
                conn.commit()
                conn.close()
                before = digest(path)
                result = Center(path).execute("migrate", {})
                self.assertFalse(result["ok"], result)
                self.assertEqual("SCHEMA_MISMATCH", result["error"]["code"])
                self.assertNotIn("schema_version", result.get("result", {}))
                self.assertEqual(version, user_version(path))
                self.assertEqual(before, digest(path))
                self.assertEqual([], [p.name for p in self.root.iterdir() if p.name.startswith(".task-")])

    def test_foreign_database_claiming_version_3_is_rejected_untouched(self):
        path = self.root / "foreign-v3.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE unrelated(value TEXT)")
        conn.execute("PRAGMA user_version=3")
        conn.commit()
        conn.close()
        before = digest(path)
        result = Center(path).execute("migrate", {})
        self.assertFalse(result["ok"], result)
        self.assertEqual("SCHEMA_MISMATCH", result["error"]["code"])
        self.assertEqual(before, digest(path))

    def test_absent_file_reports_not_initialized_and_is_not_created(self):
        path = self.root / "absent.sqlite3"
        result = Center(path).execute("migrate", {})
        self.assertFalse(result["ok"], result)
        self.assertEqual("DB_NOT_INITIALIZED", result["error"]["code"])
        self.assertFalse(path.exists())

    def test_migrate_requires_no_request_id_and_rejects_unknown_fields(self):
        result = Center(self.db).execute("migrate", {"destination": "x"})
        self.assertFalse(result["ok"], result)
        self.assertEqual("INVALID_INPUT", result["error"]["code"])
        self.assertEqual(3, user_version(self.db))


    # --- 10 -> 11：可空的指纹、依赖边与发号盘 -------------------------------

    def test_upgrade_from_v10_keeps_rows_and_allows_a_null_fingerprint(self):
        """C6：迁移不给历史行编值也不丢行，存量 prd_sha256 原样保留、状态不被改写。"""
        legacy = build_v10(self.root / "center-v10.sqlite3", self.project)
        prd_sha = hashlib.sha256((self.project / "PRD.md").read_bytes()).hexdigest()
        counted = counts(legacy, TABLES + ("merges",))
        result = Center(legacy).execute("migrate", {})
        self.assertTrue(result["ok"], result)
        receipt = result["result"]
        self.assertEqual(10, receipt["from_version"])
        self.assertEqual(SCHEMA_VERSION, receipt["schema_version"])
        self.assertTrue(receipt["migrated"])
        self.assertEqual(SCHEMA_VERSION, user_version(legacy))
        # 新增的两样：依赖边表与发号盘。
        self.assertIn("ticket_links", table_names(legacy))
        self.assertIn("next_number", columns(legacy, "projects"))
        # 行数逐表一致（ticket_links 迁移前不存在，迁移后是空表）。
        self.assertEqual(counted, counts(legacy, TABLES + ("merges",)))
        self.assertEqual({"ticket_links"} - table_names(legacy), set())
        self.assertEqual(0, counts(legacy, ("ticket_links",))["ticket_links"])
        conn = sqlite3.connect(legacy)
        try:
            rows = [(row[0], row[1]) for row in conn.execute(
                "SELECT id, prd_sha256 FROM tickets ORDER BY id")]
            states = [row[0] for row in conn.execute("SELECT state FROM tickets ORDER BY id")]
            next_number = conn.execute("SELECT next_number FROM projects").fetchone()[0]
            nullable = [row for row in conn.execute("PRAGMA table_info(tickets)") if row[1] == "prd_sha256"][0][3]
        finally:
            conn.close()
        # 存量指纹原样保留：没有把历史单的 prd_sha256 清空成 NULL。
        self.assertEqual([("AW-095", prd_sha), ("AW-096", prd_sha)], rows)
        # 也没有把存量单回溯改成 planned。
        self.assertEqual(["done", "running"], states)
        # 发号盘按已有最大号初始化：AW-096 之后是 AW-097（R5.3）。
        self.assertEqual(97, next_number)
        # 放宽的是列约束，不是数据：迁移后这一列允许为空了。
        self.assertEqual(0, nullable)
        center = Center(legacy)
        # 升级后的库立刻能发号：下一张就是 AW-097，前缀取自项目短名。
        created = center.execute("create_ticket", {
            "project_id": "ai-workspace", "title": "迁移后新开",
            "deliverable": "一份清单", "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        }, "create-after-v11")
        self.assertTrue(created["ok"], created)
        self.assertEqual("AW-097", created["result"]["id"])
        self.assertIsNone(created["result"]["prd_sha256"])
        # 历史单仍读得出来，指纹与合并记录都在。
        detail = center.execute("get_ticket", {"project_id": "ai-workspace", "ticket_id": "AW-095"})
        self.assertTrue(detail["ok"], detail)
        self.assertEqual(prd_sha, detail["result"]["prd_sha256"])
        self.assertEqual(["m1"], [item["id"] for item in detail["result"]["merges"]])
        self.assertEqual({"out": [], "in": []}, detail["result"]["links"])

    def test_second_migration_on_v10_reports_current(self):
        legacy = build_v10(self.root / "center-v10-again.sqlite3", self.project)
        self.assertTrue(Center(legacy).execute("migrate", {})["ok"])
        before = counts(legacy, TABLES + ("merges", "ticket_links"))
        again = Center(legacy).execute("migrate", {})
        self.assertTrue(again["ok"], again)
        self.assertFalse(again["result"]["migrated"])
        self.assertIn("已是最新", again["result"]["message"])
        self.assertEqual(SCHEMA_VERSION, user_version(legacy))
        self.assertEqual(before, counts(legacy, TABLES + ("merges", "ticket_links")))

    def test_v10_header_with_the_new_table_or_a_nullable_fingerprint_is_rejected(self):
        """形状守卫：声称 10 却已经有 11 的东西，一律拒且一个字节不改。"""
        for step, mutate in (
            ("new-table", lambda conn: conn.execute("CREATE TABLE ticket_links(kind TEXT)")),
            ("next-number", lambda conn: conn.execute(
                "ALTER TABLE projects ADD COLUMN next_number INTEGER NOT NULL DEFAULT 1")),
            ("nullable-fingerprint", lambda conn: conn.executescript(_NULLABLE_TICKETS_REBUILD)),
        ):
            with self.subTest(step=step):
                legacy = build_v10(self.root / f"center-v10-{step}.sqlite3", self.project)
                conn = sqlite3.connect(legacy)
                try:
                    mutate(conn)
                    conn.commit()
                finally:
                    conn.close()
                before = digest(legacy)
                result = Center(legacy).execute("migrate", {})
                self.assertFalse(result["ok"], result)
                self.assertEqual("SCHEMA_MISMATCH", result["error"]["code"])
                # 拒绝时版本号与文件字节都不许变。
                self.assertEqual(10, user_version(legacy))
                self.assertEqual(before, digest(legacy))


    # --- 11 -> 12：车次表与两个指针列 ---------------------------------------

    def test_upgrade_from_v11_adds_batches_without_backfilling(self):
        """C5：迁移不回溯——batch_key 全为 NULL、行数逐表不变、无车次的行为与迁移前一致。"""
        legacy = build_v11(self.root / "center-v11.sqlite3", self.project)
        before = counts(legacy, V11_TABLES)
        result = Center(legacy).execute("migrate", {})
        self.assertTrue(result["ok"], result)
        receipt = result["result"]
        self.assertEqual(11, receipt["from_version"])
        self.assertEqual(SCHEMA_VERSION, receipt["schema_version"])
        self.assertTrue(receipt["migrated"])
        self.assertEqual(SCHEMA_VERSION, user_version(legacy))
        # 新增的两样：车次表与两个指针列。
        self.assertIn("batches", table_names(legacy))
        self.assertIn("batch_id", columns(legacy, "tickets"))
        self.assertIn("batch_key", columns(legacy, "attempts"))
        # 行数逐表不变（batches 迁移前不存在，迁移后是空表）。
        self.assertEqual(before, counts(legacy, V11_TABLES))
        self.assertEqual(0, counts(legacy, ("batches",))["batches"])
        conn = sqlite3.connect(legacy)
        try:
            batch_ids = [row[0] for row in conn.execute("SELECT batch_id FROM tickets ORDER BY id")]
            batch_keys = [row[0] for row in conn.execute("SELECT batch_key FROM attempts ORDER BY id")]
            index = conn.execute("SELECT sql FROM sqlite_master WHERE name='active_session'").fetchone()[0]
        finally:
            conn.close()
        # 不回溯：一个值都不补，NULL 而不是空串。
        self.assertEqual([None, None], batch_ids)
        self.assertEqual([None, None], batch_keys)
        # 索引收窄成只守无车次那一半。
        self.assertIn("WHERE batch_key IS NULL", index)
        center = Center(legacy)
        # 行为与迁移前一致：同一会话在无车次单上仍旧一张，第二张被 CONFLICT 拒。
        created = center.execute("create_ticket", {
            "project_id": "ai-workspace", "title": "迁移后新开",
            "deliverable": "一份清单", "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        }, "create-after-v12")
        self.assertTrue(created["ok"], created)
        new_id = created["result"]["id"]
        self.assertEqual("AW-097", new_id)
        claimed = center.execute("claim_ticket", {
            "project_id": "ai-workspace", "ticket_id": new_id, "expected_revision": 1,
            "coordinator_session_id": "coordinator-9",
            "allow_parallel": True, "parallel_reason": "迁移验证：与在跑的 AW-096 并行",
        }, "claim-after-v12")
        self.assertTrue(claimed["ok"], claimed)
        self.assertIsNone(claimed["result"]["batch_id"])
        # executor-2 在迁移前就持着 AW-096 的活跃尝试（batch_key 为 NULL），
        # 所以它在这里领第二张无车次单仍然要被拒——这就是「行为与迁移前一致」。
        stored_root = center.execute("list_projects", {})["result"]["items"][0]["root"]
        bound = center.execute("bind_session", {
            "project_id": "ai-workspace", "ticket_id": new_id,
            "attempt_id": claimed["result"]["attempt_id"], "session_id": "executor-2",
            "project_root": stored_root,
        }, "bind-after-v12")
        self.assertFalse(bound["ok"], bound)
        self.assertEqual("CONFLICT", bound["error"]["code"])

    def test_second_migration_on_v11_reports_current(self):
        legacy = build_v11(self.root / "center-v11-again.sqlite3", self.project)
        self.assertTrue(Center(legacy).execute("migrate", {})["ok"])
        before = counts(legacy, V11_TABLES + ("batches",))
        again = Center(legacy).execute("migrate", {})
        self.assertTrue(again["ok"], again)
        self.assertFalse(again["result"]["migrated"])
        self.assertIn("已是最新", again["result"]["message"])
        self.assertEqual(SCHEMA_VERSION, user_version(legacy))
        self.assertEqual(before, counts(legacy, V11_TABLES + ("batches",)))

    def test_v11_header_with_batch_structures_is_rejected_untouched(self):
        """形状守卫：声称 11 却已经有 12 的东西，一律拒且一个字节不改。"""
        for step, mutate in (
            ("batches-table", lambda conn: conn.execute("CREATE TABLE batches(id TEXT)")),
            ("batch-key", lambda conn: conn.execute("ALTER TABLE attempts ADD COLUMN batch_key TEXT")),
            ("batch-id", lambda conn: conn.execute("ALTER TABLE tickets ADD COLUMN batch_id TEXT")),
        ):
            with self.subTest(step=step):
                legacy = build_v11(self.root / f"center-v11-{step}.sqlite3", self.project)
                conn = sqlite3.connect(legacy)
                try:
                    mutate(conn)
                    conn.commit()
                finally:
                    conn.close()
                before = digest(legacy)
                result = Center(legacy).execute("migrate", {})
                self.assertFalse(result["ok"], result)
                self.assertEqual("SCHEMA_MISMATCH", result["error"]["code"])
                self.assertEqual(11, user_version(legacy))
                self.assertEqual(before, digest(legacy))

    def test_upgrade_from_v12_adds_conflict_preview_without_backfilling(self):
        """C5：12 → 13 只多一列，存量提交的 conflict_preview 留 NULL（那时没这一项）。"""
        legacy = build_v12(self.root / "center-v12.sqlite3", self.project)
        before = counts(legacy, V12_TABLES)
        result = Center(legacy).execute("migrate", {})
        self.assertTrue(result["ok"], result)
        receipt = result["result"]
        self.assertEqual(12, receipt["from_version"])
        self.assertEqual(SCHEMA_VERSION, receipt["schema_version"])
        self.assertTrue(receipt["migrated"])
        self.assertEqual(SCHEMA_VERSION, user_version(legacy))
        self.assertIn("conflict_preview", columns(legacy, "submissions"))
        # 行数逐表不变：这一列不给任何一行写值。
        self.assertEqual(before, counts(legacy, V12_TABLES))
        conn = sqlite3.connect(legacy)
        try:
            values = [row[0] for row in conn.execute("SELECT conflict_preview FROM submissions ORDER BY id")]
        finally:
            conn.close()
        # 不回溯：NULL 而不是 `[]` 或 `{"conflicts":[]}`——补成空数组就等于声称
        # 那些历史提交都预演过且干净，那是编出来的事实。
        self.assertEqual([None], values)

    def test_second_migration_on_v12_reports_current(self):
        legacy = build_v12(self.root / "center-v12-again.sqlite3", self.project)
        self.assertTrue(Center(legacy).execute("migrate", {})["ok"])
        before = counts(legacy, V12_TABLES)
        again = Center(legacy).execute("migrate", {})
        self.assertTrue(again["ok"], again)
        self.assertFalse(again["result"]["migrated"])
        self.assertIn("已是最新", again["result"]["message"])
        self.assertEqual(SCHEMA_VERSION, user_version(legacy))
        self.assertEqual(before, counts(legacy, V12_TABLES))

    def test_v12_header_with_conflict_preview_is_rejected_untouched(self):
        """形状守卫：声称 12 却已经有 13 的那一列，一律拒且一个字节不改。"""
        legacy = build_v12(self.root / "center-v12-shape.sqlite3", self.project)
        conn = sqlite3.connect(legacy)
        try:
            conn.execute("ALTER TABLE submissions ADD COLUMN conflict_preview TEXT")
            conn.commit()
        finally:
            conn.close()
        before = digest(legacy)
        result = Center(legacy).execute("migrate", {})
        self.assertFalse(result["ok"], result)
        self.assertEqual("SCHEMA_MISMATCH", result["error"]["code"])
        self.assertEqual(12, user_version(legacy))
        self.assertEqual(before, digest(legacy))


    def test_upgrade_from_v13_adds_owner_session_id_and_backfills_it(self):
        """C5：14 → 15 只多一列（owner）；13 → 14 是 AW-106 先落地的 `requester_session_id` 与 `notices.message_id`。存量单的 owner 填那一代的协调会话，没有尝试的单留 NULL。"""
        legacy = build_v13(self.root / "center-v13.sqlite3", self.project)
        before = counts(legacy, V12_TABLES)
        result = Center(legacy).execute("migrate", {})
        self.assertTrue(result["ok"], result)
        receipt = result["result"]
        self.assertEqual(13, receipt["from_version"])
        self.assertEqual(SCHEMA_VERSION, receipt["schema_version"])
        self.assertTrue(receipt["migrated"])
        self.assertEqual(SCHEMA_VERSION, user_version(legacy))
        self.assertIn("owner_session_id", columns(legacy, "tickets"))
        # 行数逐表不变：这一列只填值，不加行、不删行。
        self.assertEqual(before, counts(legacy, V12_TABLES))
        conn = sqlite3.connect(legacy)
        try:
            owners = {row[0]: row[1] for row in
                      conn.execute("SELECT id, owner_session_id FROM tickets ORDER BY id")}
            origin = {row[0]: row[1] for row in conn.execute(
                "SELECT ticket_id, coordinator_session_id FROM attempts ORDER BY ticket_id")}
        finally:
            conn.close()
        # 有当前尝试的两张单，owner 就是它们当前那一代的协调会话——「当时的责任人」。
        self.assertEqual({"AW-095": "coordinator-1", "AW-096": "coordinator-1", "AW-097": None}, owners)
        # origin 一个字都没动：14 不重写历史，只多知道「现在谁负责」。
        self.assertEqual({"AW-095": "coordinator-1", "AW-096": "coordinator-1"}, origin)

    def test_second_migration_on_v13_reports_current(self):
        legacy = build_v13(self.root / "center-v13-again.sqlite3", self.project)
        self.assertTrue(Center(legacy).execute("migrate", {})["ok"])
        before = counts(legacy, V12_TABLES)
        again = Center(legacy).execute("migrate", {})
        self.assertTrue(again["ok"], again)
        self.assertFalse(again["result"]["migrated"])
        self.assertIn("已是最新", again["result"]["message"])
        self.assertEqual(SCHEMA_VERSION, user_version(legacy))
        self.assertEqual(before, counts(legacy, V12_TABLES))

    def test_v13_header_with_owner_session_id_is_rejected_untouched(self):
        """形状守卫：声称 13 却已经有 14 的那一列，一律拒且一个字节不改。"""
        legacy = build_v13(self.root / "center-v13-shape.sqlite3", self.project)
        conn = sqlite3.connect(legacy)
        try:
            conn.execute("ALTER TABLE tickets ADD COLUMN owner_session_id TEXT")
            conn.commit()
        finally:
            conn.close()
        before = digest(legacy)
        result = Center(legacy).execute("migrate", {})
        self.assertFalse(result["ok"], result)
        self.assertEqual("SCHEMA_MISMATCH", result["error"]["code"])
        self.assertEqual(13, user_version(legacy))
        self.assertEqual(before, digest(legacy))


    def test_v14_header_with_owner_session_id_is_rejected_untouched(self):
        """形状守卫：声称 14 却已经有 15 的那一列，一律拒且一个字节不改。"""
        legacy = build_v14(self.root / "center-v14-shape.sqlite3", self.project)
        conn = sqlite3.connect(legacy)
        try:
            conn.execute("ALTER TABLE tickets ADD COLUMN owner_session_id TEXT")
            conn.commit()
        finally:
            conn.close()
        before = digest(legacy)
        result = Center(legacy).execute("migrate", {})
        self.assertFalse(result["ok"], result)
        self.assertEqual("SCHEMA_MISMATCH", result["error"]["code"])
        self.assertEqual(14, user_version(legacy))
        self.assertEqual(before, digest(legacy))

    def test_upgrade_from_v15_adds_merge_backfill_columns_without_backfilling(self):
        """C6：15 → 16 只多四列；存量合并行的 source / verified_by / verified_at / note 留 NULL。"""
        legacy = build_v15(self.root / "center-v15.sqlite3", self.project)
        before = counts(legacy, V12_TABLES)
        result = Center(legacy).execute("migrate", {})
        self.assertTrue(result["ok"], result)
        receipt = result["result"]
        self.assertEqual(15, receipt["from_version"])
        self.assertEqual(SCHEMA_VERSION, receipt["schema_version"])
        self.assertTrue(receipt["migrated"])
        self.assertEqual(SCHEMA_VERSION, user_version(legacy))
        self.assertEqual(16, user_version(legacy))
        for column in ("source", "verified_by", "verified_at", "note"):
            self.assertIn(column, columns(legacy, "merges"))
        # 行数逐表不变：只加列，不加行、不删行。
        self.assertEqual(before, counts(legacy, V12_TABLES))
        conn = sqlite3.connect(legacy)
        try:
            row = conn.execute("SELECT source, verified_by, verified_at, note FROM merges WHERE id='m1'").fetchone()
        finally:
            conn.close()
        # 「那时没有这一项」不是「那时是 auto_merge」：四列一个都不填。
        self.assertEqual((None, None, None, None), row)

    def test_second_migration_on_v15_reports_current(self):
        legacy = build_v15(self.root / "center-v15-again.sqlite3", self.project)
        self.assertTrue(Center(legacy).execute("migrate", {})["ok"])
        again = Center(legacy).execute("migrate", {})
        self.assertTrue(again["ok"], again)
        self.assertFalse(again["result"]["migrated"])
        self.assertIn("已是最新", again["result"]["message"])
        self.assertEqual(16, user_version(legacy))

    def test_v15_header_with_all_backfill_columns_completes_the_migration(self):
        """四列都在、版本号还停在 15：补完版本，不报形状错、不改任何值。

        这就是本单在正式库上真实遇到的现场——另一个会话为了让 master 上的旧代码还能
        读库，把版本号单独改回 15。迁移应当把版本补完，而不是把库锁在门外。
        """
        legacy = build_v15(self.root / "center-v15-complete.sqlite3", self.project)
        conn = sqlite3.connect(legacy)
        try:
            for column in ("source", "verified_by", "verified_at", "note"):
                conn.execute(f"ALTER TABLE merges ADD COLUMN {column} TEXT")
            conn.execute("UPDATE merges SET source='backfill', verified_at='2026-11-22T00:00:00+00:00' WHERE id='m1'")
            conn.commit()
        finally:
            conn.close()
        before = counts(legacy, V12_TABLES)
        result = Center(legacy).execute("migrate", {})
        self.assertTrue(result["ok"], result)
        self.assertEqual(15, result["result"]["from_version"])
        self.assertEqual(16, user_version(legacy))
        self.assertEqual(before, counts(legacy, V12_TABLES))
        conn = sqlite3.connect(legacy)
        try:
            row = conn.execute("SELECT source, verified_at FROM merges WHERE id='m1'").fetchone()
        finally:
            conn.close()
        # 已写下的值原样保留：补的是版本号，不是内容。
        self.assertEqual(("backfill", "2026-11-22T00:00:00+00:00"), row)

    def test_v15_header_with_merge_source_is_rejected_untouched(self):
        """形状守卫：声称 15 却已经有 16 的那一列，一律拒且一个字节不改。"""
        legacy = build_v15(self.root / "center-v15-shape.sqlite3", self.project)
        conn = sqlite3.connect(legacy)
        try:
            conn.execute("ALTER TABLE merges ADD COLUMN source TEXT")
            conn.commit()
        finally:
            conn.close()
        before = digest(legacy)
        result = Center(legacy).execute("migrate", {})
        self.assertFalse(result["ok"], result)
        self.assertEqual("SCHEMA_MISMATCH", result["error"]["code"])
        self.assertEqual(15, user_version(legacy))
        self.assertEqual(before, digest(legacy))



if __name__ == "__main__":
    unittest.main()