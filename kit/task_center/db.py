"""SQLite storage: explicit initialization and atomic command receipts."""
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class Problem(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def require(condition, code, message):
    if not condition:
        raise Problem(code, message)


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def now():
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
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


def expected_tables(conn):
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return names


REQUIRED_TABLES = {"projects", "tickets", "attempts", "submissions", "reviews", "events", "merges", "requests",
                   "questions", "answers", "notices"}

# Version 2 adds the PRD snapshot columns on attempts. Version 3 adds tickets.deliverable.
# Version 4 adds attempts.coordinator_session_id so a coordinator cannot bind its own claim.
# Version 5 adds merges so an accepted ticket can carry a queryable "landed" record.
# Version 6 adds questions/answers so "waiting for one sentence" is a recorded state, and
# widens the active_session predicate to include 'blocked' (PRD-005 4.4).
# Version 7 adds attempts.coordinator_workdir and the notices table so a handoff receipt that
# was or was not sent is a recorded fact, not something a coordinator has to remember (PRD-009 R5).
# Version 8 adds notices.target_session_id and notices.wake_kind so "who it was sent to" and
# "how the host treated it" are recorded too; a wake_kind of created means the host started a
# new task instead of delivering, which is recorded as a failed notice (PRD-009 R5, AW-060).
# Version 9 adds submissions.head_sha and merges.reviewed_sha so "which tree was accepted" and
# "which tree is being merged" are two recorded values that can be compared (R-009 goal 2).
# Both columns are nullable on purpose: rows written before this version have no sha, and
# inventing one would turn a guess into a fact. `merge_ticket` therefore requires the literal
# `unverified` for such a ticket instead of letting it through silently.
# Only the 3 -> 4 -> 5 -> 6 -> 7 -> 8 -> 9 upgrades have known paths; everything else is rejected loudly.
SCHEMA_VERSION = 9

REQUIRED_COLUMNS = {
    "tickets": {"id", "project_id", "title", "deliverable", "prd_path", "prd_sha256", "criteria", "revision", "state", "current_attempt"},
    "attempts": {"id", "ticket_id", "revision", "prd_path", "prd_sha256", "state", "session_id", "coordinator_session_id",
                 "coordinator_workdir", "created_at"},
    "submissions": {"id", "attempt_id", "summary", "artifacts", "evidence", "risks", "created_at", "head_sha"},
    "merges": {"id", "ticket_id", "commit_sha", "target_branch", "tests", "created_at", "reviewed_sha"},
    "questions": {"id", "ticket_id", "attempt_id", "question", "options", "state", "asked_at"},
    "answers": {"id", "question_id", "answer", "answered_by", "answered_by_session_id", "created_at"},
    "notices": {"id", "ticket_id", "attempt_id", "origin_session_id", "status", "detail", "created_at",
                "target_session_id", "wake_kind"},
}

# 冻结的 v3 形状：升级前先核对它确实是本工具的旧库，而不是碰巧带 user_version=3 的别的文件。
# 三条冻结形状都写全，不用 REQUIRED_TABLES 做集合减法——那会把「以后新增的表」
# 自动算进旧版本应该有的形状里，升级链一加级就拦下本该放行的旧库。
V3_REQUIRED_TABLES = {"projects", "tickets", "attempts", "submissions", "reviews", "events", "requests"}
V3_REQUIRED_COLUMNS = {
    "tickets": REQUIRED_COLUMNS["tickets"],
    "attempts": REQUIRED_COLUMNS["attempts"] - {"coordinator_session_id", "coordinator_workdir"},
}

# v4 有 coordinator_session_id、还没有 merges 与 coordinator_workdir；同样是冻结形状。
V4_REQUIRED_TABLES = V3_REQUIRED_TABLES
V4_REQUIRED_COLUMNS = {
    "tickets": REQUIRED_COLUMNS["tickets"],
    "attempts": REQUIRED_COLUMNS["attempts"] - {"coordinator_workdir"},
}

# v5 有 merges（但还没有 reviewed_sha），还没有 questions/answers，active_session 谓词不含 blocked。
V5_REQUIRED_TABLES = V3_REQUIRED_TABLES | {"merges"}
V5_REQUIRED_COLUMNS = {**V4_REQUIRED_COLUMNS, "merges": REQUIRED_COLUMNS["merges"] - {"reviewed_sha"}}

# v6 有 questions/answers、还没有 notices 与 coordinator_workdir。
V6_REQUIRED_TABLES = V5_REQUIRED_TABLES | {"questions", "answers"}
V6_REQUIRED_COLUMNS = {**V5_REQUIRED_COLUMNS, "questions": REQUIRED_COLUMNS["questions"],
                       "answers": REQUIRED_COLUMNS["answers"]}

# v7 有 notices 与 coordinator_workdir，notices 还没有 target_session_id / wake_kind 两列。
V7_REQUIRED_TABLES = V6_REQUIRED_TABLES | {"notices"}
V7_REQUIRED_COLUMNS = {**V6_REQUIRED_COLUMNS, "notices": REQUIRED_COLUMNS["notices"] - {"target_session_id", "wake_kind"}}

# v8 有 notices 的两列，还没有 submissions.head_sha 与 merges.reviewed_sha。
V8_REQUIRED_TABLES = V7_REQUIRED_TABLES
V8_REQUIRED_COLUMNS = {**V7_REQUIRED_COLUMNS, "submissions": REQUIRED_COLUMNS["submissions"] - {"head_sha"}}


def column_names(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def validate_columns(conn, required, tables=None):
    require((REQUIRED_TABLES if tables is None else tables) <= expected_tables(conn), "SCHEMA_MISMATCH",
            "Database is missing required task-center tables")
    for table, columns in required.items():
        require(columns <= column_names(conn, table), "SCHEMA_MISMATCH",
                f"Table {table} is missing required columns")


# 每一步升级：升级前该版本应有的形状、升级语句、以及「声称旧版本却已经有新东西」的识别方式。
MIGRATIONS = {
    3: {
        "tables": V3_REQUIRED_TABLES,
        "columns": V3_REQUIRED_COLUMNS,
        "statements": ["ALTER TABLE attempts ADD COLUMN coordinator_session_id TEXT"],
        "already_applied": lambda conn: "coordinator_session_id" in column_names(conn, "attempts"),
        "already_applied_message": "Database claims an older version but already has the newer column; inspect it, do not migrate",
    },
    4: {
        "tables": V4_REQUIRED_TABLES,
        "columns": V4_REQUIRED_COLUMNS,
        "statements": [
            "CREATE TABLE merges("
            " id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL UNIQUE REFERENCES tickets(id),"
            " commit_sha TEXT NOT NULL, target_branch TEXT NOT NULL, tests TEXT NOT NULL, created_at TEXT NOT NULL)",
        ],
        "already_applied": lambda conn: "merges" in expected_tables(conn),
        "already_applied_message": "Database claims version 4 but already has the merges table; inspect it, do not migrate",
    },
    5: {
        "tables": V5_REQUIRED_TABLES,
        "columns": V5_REQUIRED_COLUMNS,
        "statements": [
            "CREATE TABLE questions("
            " id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id),"
            " attempt_id TEXT NOT NULL REFERENCES attempts(id),"
            " question TEXT NOT NULL, options TEXT NOT NULL, state TEXT NOT NULL, asked_at TEXT NOT NULL)",
            "CREATE UNIQUE INDEX one_open_question ON questions(attempt_id) WHERE state='open'",
            "CREATE TABLE answers("
            " id TEXT PRIMARY KEY, question_id TEXT NOT NULL UNIQUE REFERENCES questions(id),"
            " answer TEXT NOT NULL, answered_by TEXT NOT NULL, answered_by_session_id TEXT, created_at TEXT NOT NULL)",
            "CREATE INDEX questions_ticket ON questions(ticket_id, asked_at)",
            # 索引谓词要跟着 blocked 一起变；不 DROP 就重建会撞同名索引。
            "DROP INDEX IF EXISTS active_session",
            "CREATE UNIQUE INDEX active_session ON attempts(session_id)"
            " WHERE session_id IS NOT NULL AND state IN ('blocked','running','submitted')",
        ],
        "already_applied": lambda conn: "questions" in expected_tables(conn),
        "already_applied_message": "Database claims version 5 but already has the questions table; inspect it, do not migrate",
    },
    6: {
        "tables": V6_REQUIRED_TABLES,
        "columns": V6_REQUIRED_COLUMNS,
        "statements": [
            "ALTER TABLE attempts ADD COLUMN coordinator_workdir TEXT",
            "CREATE TABLE notices("
            " id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id),"
            " attempt_id TEXT NOT NULL REFERENCES attempts(id), origin_session_id TEXT NOT NULL,"
            " status TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL)",
            # 一张单最多一条 sent 落成部分唯一索引：靠代码自觉会在并发里漏一次。
            "CREATE UNIQUE INDEX one_sent_notice ON notices(ticket_id) WHERE status='sent'",
            "CREATE INDEX notices_ticket ON notices(ticket_id, created_at)",
        ],
        "already_applied": lambda conn: ("notices" in expected_tables(conn)
                                         or "coordinator_workdir" in column_names(conn, "attempts")),
        "already_applied_message": "Database claims version 6 but already has notices or coordinator_workdir; inspect it, do not migrate",
    },
    7: {
        "tables": V7_REQUIRED_TABLES,
        "columns": V7_REQUIRED_COLUMNS,
        "statements": [
            # 存量行留空串=「历史未记」：那时还没有这两项，补一个猜测值会变成推定事实。
            "ALTER TABLE notices ADD COLUMN target_session_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE notices ADD COLUMN wake_kind TEXT NOT NULL DEFAULT ''",
        ],
        "already_applied": lambda conn: ("target_session_id" in column_names(conn, "notices")
                                         or "wake_kind" in column_names(conn, "notices")),
        "already_applied_message": "Database claims version 7 but already has the newer notices columns; inspect it, do not migrate",
    },
    8: {
        "tables": V8_REQUIRED_TABLES,
        "columns": V8_REQUIRED_COLUMNS,
        "statements": [
            # 存量行留 NULL=「那时没有这一项」：补一个猜测值会变成推定事实。
            "ALTER TABLE submissions ADD COLUMN head_sha TEXT",
            "ALTER TABLE merges ADD COLUMN reviewed_sha TEXT",
        ],
        "already_applied": lambda conn: ("head_sha" in column_names(conn, "submissions")
                                         or "reviewed_sha" in column_names(conn, "merges")),
        "already_applied_message": "Database claims version 8 but already has submissions.head_sha or merges.reviewed_sha; inspect it, do not migrate",
    },
}


def upgrade_chain():
    """已知升级链的文案：从 MIGRATIONS 的键推出来，不再手写「3 -> 4 -> 5」，免得加一级就成假话。"""
    return " -> ".join([str(version) for version in sorted(MIGRATIONS)] + [str(SCHEMA_VERSION)])


def validate_schema(conn):
    """One口径校验：表齐全且关键列齐全，避免把外部库当成工单中心。"""
    validate_columns(conn, REQUIRED_COLUMNS)


def initialize(path):
    path = Path(path).absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = expected_tables(conn)
        if version == SCHEMA_VERSION:
            validate_schema(conn)
            conn.commit()
            return {"schema_version": SCHEMA_VERSION, "database": str(path)}
        require(version == 0 and not tables, "SCHEMA_MISMATCH", "Database is not an empty task-center database")
        # executescript commits implicitly; execute statements individually to retain this transaction.
        for statement in SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
        # Only touch journal mode after the file is proven empty and initialization committed.
        conn.execute("PRAGMA journal_mode=WAL")
        return {"schema_version": SCHEMA_VERSION, "database": str(path)}
    finally:
        conn.close()


def backup_database(source, destination):
    target = Path(destination)
    require(target.is_absolute() and target.parent.is_dir(), "INVALID_INPUT", "Backup requires an absolute path in an existing directory")
    require(not os.path.lexists(target), "BACKUP_EXISTS", "Backup destination already exists; no overwrite is allowed")
    temp_path = None
    try:
        with connection(source) as src:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".task-backup-", suffix=".tmp", delete=False) as temp:
                temp_path = Path(temp.name)
            dst = sqlite3.connect(temp_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
            # Publish a complete snapshot atomically without overwriting an existing destination.
            try:
                os.link(temp_path, target)
            except FileExistsError as exc:
                raise Problem("BACKUP_EXISTS", "Backup destination already exists") from exc
        return {"destination": str(target), "schema_version": SCHEMA_VERSION}
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def migrate_database(path):
    """只走已知升级路径。拒绝时不开事务、不写任何字节。"""
    path = Path(path).absolute()
    require(path.is_file(), "DB_NOT_INITIALIZED", "Initialize the intended database explicitly first")
    conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10, isolation_level=None)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            validate_schema(conn)
            return {"database": str(path), "schema_version": SCHEMA_VERSION, "from_version": SCHEMA_VERSION,
                    "migrated": False, "message": "已是最新，没有修改任何行"}
        require(version in MIGRATIONS, "SCHEMA_MISMATCH",
                f"Unsupported schema version {version}; known upgrades are {upgrade_chain()}")
        step = MIGRATIONS[version]
        validate_columns(conn, step["columns"], step["tables"])
        require(not step["already_applied"](conn), "SCHEMA_MISMATCH", step["already_applied_message"])
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 事务内再确认一次，避免两次读之间版本被换掉。
            require(conn.execute("PRAGMA user_version").fetchone()[0] == version, "SCHEMA_MISMATCH",
                    "Schema version changed while migrating")
            # 逐级升级：3 -> 4 -> 5 -> 6 -> 7 -> 8，每一级只跑自己那几条语句。
            current = version
            while current < SCHEMA_VERSION:
                for statement in MIGRATIONS[current]["statements"]:
                    conn.execute(statement)
                current += 1
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            validate_schema(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {"database": str(path), "schema_version": SCHEMA_VERSION, "from_version": version,
                "migrated": True, "message": f"已从 {version} 升级到 {SCHEMA_VERSION}，原有记录全部保留"}
    finally:
        conn.close()


@contextmanager
def connection(path, write=False):
    path = Path(path).absolute()
    require(path.is_file(), "DB_NOT_INITIALIZED", "Initialize the intended database explicitly first")
    conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        require(conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION,
                "SCHEMA_MISMATCH", "Unsupported task-center schema")
        validate_schema(conn)
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
