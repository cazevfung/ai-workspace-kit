"""schema 3 → 8 的升级：只走已知的升级链，且拒绝时一个字节都不改。

正式库 `{{WORKSPACE}}/.data/task-center.sqlite3` 里已经有工单、尝试与验收记录，
不能因为多了一列或一张表就重建。这里自己合成旧版库（把后来才有的列与表都去掉，
写成冻结的 v3 / v5 / v6 形状），再对副本跑 `migrate`，逐条核对「版本变了、列和表多了、
行数没少、工单还读得出来」。

拒绝路径同样重要：`migrate` 不是「尽力而为」的修复工具。版本不认识、或文件根本
不是本工具的库，它必须什么都不做，让调用方去看清楚那是什么文件。
"""
import hashlib
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from task_center import Center

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
        self.assertEqual(9, receipt["schema_version"])
        self.assertTrue(receipt["migrated"])
        self.assertEqual(9, user_version(self.db))
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
        self.assertEqual(9, result["result"]["schema_version"])
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
        self.assertEqual(9, user_version(self.db))
        self.assertEqual(before, counts(self.db))

    # --- 5 -> 6：问答两张表与 blocked 索引谓词 ------------------------------

    def test_upgrade_from_v5_adds_question_tables_and_widens_the_index(self):
        legacy = build_v5(self.root / "center-v5.sqlite3", self.project)
        before = counts(legacy)
        result = Center(legacy).execute("migrate", {})
        self.assertTrue(result["ok"], result)
        receipt = result["result"]
        self.assertEqual(5, receipt["from_version"])
        self.assertEqual(9, receipt["schema_version"])
        self.assertTrue(receipt["migrated"])
        self.assertEqual(9, user_version(legacy))
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
        self.assertEqual(9, receipt["schema_version"])
        self.assertTrue(receipt["migrated"])
        self.assertEqual(9, user_version(legacy))
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
        self.assertEqual(9, receipt["schema_version"])
        self.assertTrue(receipt["migrated"])
        self.assertEqual(9, user_version(legacy))
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
        self.assertEqual(9, receipt["schema_version"])
        self.assertTrue(receipt["migrated"])
        self.assertEqual(9, user_version(legacy))
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


if __name__ == "__main__":
    unittest.main()
