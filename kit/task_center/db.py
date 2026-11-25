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
CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, root TEXT NOT NULL UNIQUE,
 next_number INTEGER NOT NULL DEFAULT 1);
CREATE TABLE batches(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 resource_key TEXT NOT NULL, owner_session_id TEXT,
 planned_at TEXT NOT NULL, closed_at TEXT);
CREATE TABLE tickets(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), title TEXT NOT NULL,
 deliverable TEXT NOT NULL,
 prd_path TEXT NOT NULL, prd_sha256 TEXT, criteria TEXT NOT NULL,
 revision INTEGER NOT NULL, criteria_version INTEGER NOT NULL DEFAULT 1,
 state TEXT NOT NULL, current_attempt TEXT, created_at TEXT NOT NULL,
 batch_id TEXT REFERENCES batches(id),
 owner_session_id TEXT, requester_session_id TEXT);
CREATE TABLE attempts(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id), revision INTEGER NOT NULL,
 criteria_version INTEGER NOT NULL DEFAULT 1,
 prd_path TEXT NOT NULL, prd_sha256 TEXT NOT NULL,
 state TEXT NOT NULL, session_id TEXT, coordinator_session_id TEXT, coordinator_workdir TEXT,
 created_at TEXT NOT NULL, batch_key TEXT);
CREATE UNIQUE INDEX active_session ON attempts(session_id)
 WHERE batch_key IS NULL AND session_id IS NOT NULL AND state IN ('blocked','running','submitted');
CREATE TABLE submissions(
 id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id),
 summary TEXT NOT NULL, artifacts TEXT NOT NULL, evidence TEXT NOT NULL, risks TEXT NOT NULL,
 created_at TEXT NOT NULL, head_sha TEXT, base_sha TEXT, conflict_preview TEXT);
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
 reviewed_sha TEXT,
 source TEXT, verified_by TEXT, verified_at TEXT, note TEXT);
CREATE TABLE questions(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id),
 attempt_id TEXT NOT NULL REFERENCES attempts(id),
 question TEXT NOT NULL, options TEXT NOT NULL, state TEXT NOT NULL, asked_at TEXT NOT NULL);
CREATE UNIQUE INDEX one_open_question ON questions(attempt_id) WHERE state='open';
CREATE TABLE notices(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id),
 attempt_id TEXT NOT NULL REFERENCES attempts(id), origin_session_id TEXT NOT NULL,
 status TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL,
 target_session_id TEXT NOT NULL DEFAULT '', wake_kind TEXT NOT NULL DEFAULT '', message_id TEXT);
CREATE UNIQUE INDEX one_sent_notice ON notices(ticket_id) WHERE status='sent';
CREATE INDEX notices_ticket ON notices(ticket_id, created_at);
CREATE TABLE answers(
 id TEXT PRIMARY KEY, question_id TEXT NOT NULL UNIQUE REFERENCES questions(id),
 answer TEXT NOT NULL, answered_by TEXT NOT NULL, answered_by_session_id TEXT, created_at TEXT NOT NULL);
CREATE INDEX questions_ticket ON questions(ticket_id, asked_at);
CREATE INDEX events_ticket ON events(ticket_id, id);
CREATE INDEX tickets_project ON tickets(project_id, created_at);
CREATE TABLE requests(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL);
CREATE TABLE ticket_links(
 from_ticket TEXT NOT NULL REFERENCES tickets(id), kind TEXT NOT NULL,
 to_ticket TEXT NOT NULL REFERENCES tickets(id), created_at TEXT NOT NULL,
 PRIMARY KEY (from_ticket, kind, to_ticket),
 CHECK (kind IN ('blocked_by','related','rides_with')));
CREATE INDEX ticket_links_to ON ticket_links(to_ticket, kind);
"""


def expected_tables(conn):
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return names


REQUIRED_TABLES = {"projects", "batches", "tickets", "attempts", "submissions", "reviews", "events", "merges",
                   "requests", "questions", "answers", "notices", "ticket_links"}

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
# Version 10 splits "the criteria changed" from "this attempt is dead" (PRD-013 R6/R7):
# tickets.criteria_version and attempts.criteria_version count criteria revisions only, while
# tickets.revision keeps counting generations (recover / cancel / restart / reopen).
# submissions.base_sha records which commit the submitted tree branched from. Existing rows get
# criteria_version = 1 (they were dispatched against the only criteria set that existed) and
# base_sha = NULL (no commit was recorded then; a guessed sha would be a fact nobody verified).
#
# Version 11 gives "not started yet" a place in the database (PRD-014 R1/R2/R3/R5):
# tickets.prd_sha256 becomes nullable so a planned ticket carries no fingerprint until it is
# claimed; projects.next_number turns the ticket id into a number the database issues instead of
# one the caller announces; ticket_links records blocked_by / related / rides_with edges. The
# tickets table has to be rebuilt to drop NOT NULL from prd_sha256 — that is the one step in this
# chain that is not a plain ALTER, and it copies every row verbatim (no value is invented: a
#
# Version 12 gives "several tickets in one trip" a place in the database (PRD-014 R4): batches holds the
# window (resource_key + owner_session_id + planned_at/closed_at), tickets.batch_id says which trip a ticket
# rides in, and attempts.batch_key copies it at claim time so the active_session index can be narrowed to
# `batch_key IS NULL`. That narrowing is the whole point: a trip's tickets are outside the one-session-one-
# ticket index, so one session may hold several active attempts in the same trip; ownership of a trip is not
# an index but a runtime check in claim_ticket against batches.owner_session_id (BATCH_OWNED_BY_OTHER).
# Nothing is backfilled: existing tickets get batch_id NULL and existing attempts batch_key NULL, so a
# migrated database behaves exactly like it did before (one session, one ticket without a trip).
#
# Version 13 records the merge preview taken at submit time (PRD-015 R2): submissions.conflict_preview keeps
# `{target_branch, target_sha, command, exit_code, conflicts}` as JSON text. An empty `conflicts` array means
# "the preview ran and the merge is clean"; NULL means "no preview" — either the row predates this column or the
# preview could not be computed (not a repository, head_sha not local, git unavailable). The two are kept apart
# on purpose: writing an empty array for an uncomputed preview would claim every historical submission had
# been previewed and come out clean.
#
# Version 14 gives "who asked for this ticket" and "which message carried the receipt" a place in the
# database (PRD-015 R5): tickets.requester_session_id records the session that opened the ticket, and
# notices.message_id records the id of the receipt message so a reader can pull that conversation with
# `get_chat_history`. Both are nullable on purpose: rows written before this version have no such value,
# and the coordinator session is NOT a substitute for the requester — filling one in with the other
# would turn "nobody recorded it" into a fact nobody checked. Message ids are ids only: no body, no
# deeplink (a `cindy://...?message=` link was never verified to open anything).
#
# Version 15 splits two things one column was carrying (AW-114): `attempts.coordinator_session_id` says who
# dispatched this attempt (origin — an immutable historical fact) while `tickets.owner_session_id` says who is
# responsible for closing it out now (owner — mutable, transferable with `take_over_ticket`). Authorization for
# `record_notice` / `merge_ticket` reads the owner, so an archived dispatcher no longer locks an accepted ticket
# out of the merge (AW-106). The migration backfills the owner from the ticket's current attempt — "who was
# responsible then", not a claim that the same session is still alive. Tickets with no current attempt keep NULL:
# inventing an owner for them would name a session nobody ever dispatched to.
#
# Version 16 answers "was this merge record written then, or filled in later?" (AW-116):
# merges.source says who wrote it (`auto_merge` = the normal merge path, `backfill` = a later
# repair of a ticket merged before records were kept), merges.verified_by holds the exact command
# that was re-run to prove the tree is in the target branch, merges.verified_at says when that
# check ran, and merges.note carries the repair's reason. All four are nullable on purpose: rows
# written before this version keep NULL, which means "that field did not exist then" — backfilling
# them with `auto_merge` would turn a fact nobody recorded into one. The ticket originally called
# this step 14 -> 15, but 15 had already been taken by AW-114, so it lands as 15 -> 16.
#
# Only the 3 -> 4 -> 5 -> … -> 14 -> 15 -> 16 upgrades have known paths; everything else is rejected loudly.
SCHEMA_VERSION = 16

# 冻结形状的起点：v3 的 tickets / attempts 列。后面每一级的冻结形状都从这两个集合长出来，
# 而不是从 REQUIRED_COLUMNS 做减法——后者会把「以后新增的列」自动算进旧版本应该有的形状里，
# 升级链一加级就拦下本该放行的旧库。
V3_TICKET_COLUMNS = {"id", "project_id", "title", "deliverable", "prd_path", "prd_sha256", "criteria",
                     "revision", "state", "current_attempt", "created_at"}
V3_ATTEMPT_COLUMNS = {"id", "ticket_id", "revision", "prd_path", "prd_sha256", "state", "session_id",
                      "created_at"}
# notices 在 13 之前的列（从 v7 起就这么多）；14 加的 `message_id` 不在里面。
# 冻结形状从这里减，不从 REQUIRED_COLUMNS 减：后者会把这以后新增的列自动算进 v7 应有
# 的形状里，于是升到 14 的时候，本该放行的 v7 旧库会被当成缺列拒掉——这一下当场踩过。
NOTICES_BEFORE_MESSAGE_ID = {"id", "ticket_id", "attempt_id", "origin_session_id", "status", "detail",
                            "created_at", "target_session_id", "wake_kind"}

# 16 之前的 merges 形状（v9 起七列）。这一组单独冻结：直接从 REQUIRED_COLUMNS["merges"]
# 做减法的老写法会把以后新增的四列自动算进 v5/v8 应有的形状里，升级链一加级就误拒旧库。
MERGES_BEFORE_SOURCE = {"id", "ticket_id", "commit_sha", "target_branch", "tests", "created_at",
                        "reviewed_sha"}
# v5 的 merges 还没有 reviewed_sha（9 才加）。
MERGES_BEFORE_REVIEWED_SHA = MERGES_BEFORE_SOURCE - {"reviewed_sha"}
# 16 加的四列（顺序即后补的顺序）。
MERGE_BACKFILL_COLUMNS = ("source", "verified_by", "verified_at", "note")

REQUIRED_COLUMNS = {
    "projects": {"id", "name", "root", "next_number"},
    "batches": {"id", "project_id", "resource_key", "owner_session_id", "planned_at", "closed_at"},
    "tickets": V3_TICKET_COLUMNS | {"criteria_version", "batch_id", "owner_session_id",
                                    "requester_session_id"},
    "attempts": V3_ATTEMPT_COLUMNS | {"coordinator_session_id", "coordinator_workdir", "criteria_version",
                                   "batch_key"},
    "submissions": {"id", "attempt_id", "summary", "artifacts", "evidence", "risks", "created_at", "head_sha",
                    "base_sha", "conflict_preview"},
    "merges": MERGES_BEFORE_SOURCE | {"source", "verified_by", "verified_at", "note"},
    "questions": {"id", "ticket_id", "attempt_id", "question", "options", "state", "asked_at"},
    "answers": {"id", "question_id", "answer", "answered_by", "answered_by_session_id", "created_at"},
    "notices": NOTICES_BEFORE_MESSAGE_ID | {"message_id"},
    "ticket_links": {"from_ticket", "kind", "to_ticket", "created_at"},
}

# 冻结的 v3 形状：升级前先核对它确实是本工具的旧库，而不是碰巧带 user_version=3 的别的文件。
# 三条冻结形状都写全，不用 REQUIRED_TABLES 做集合减法——那会把「以后新增的表」
# 自动算进旧版本应该有的形状里，升级链一加级就拦下本该放行的旧库。
V3_REQUIRED_TABLES = {"projects", "tickets", "attempts", "submissions", "reviews", "events", "requests"}
V3_REQUIRED_COLUMNS = {"tickets": V3_TICKET_COLUMNS, "attempts": V3_ATTEMPT_COLUMNS}

# v4 有 coordinator_session_id、还没有 merges 与 coordinator_workdir；同样是冻结形状。
V4_REQUIRED_TABLES = V3_REQUIRED_TABLES
V4_REQUIRED_COLUMNS = {
    "tickets": V3_TICKET_COLUMNS,
    "attempts": V3_ATTEMPT_COLUMNS | {"coordinator_session_id"},
}

# v5 有 merges（但还没有 reviewed_sha），还没有 questions/answers，active_session 谓词不含 blocked。
V5_REQUIRED_TABLES = V3_REQUIRED_TABLES | {"merges"}
V5_REQUIRED_COLUMNS = {**V4_REQUIRED_COLUMNS, "merges": MERGES_BEFORE_REVIEWED_SHA}

# v6 有 questions/answers、还没有 notices 与 coordinator_workdir。
V6_REQUIRED_TABLES = V5_REQUIRED_TABLES | {"questions", "answers"}
V6_REQUIRED_COLUMNS = {**V5_REQUIRED_COLUMNS, "questions": REQUIRED_COLUMNS["questions"],
                       "answers": REQUIRED_COLUMNS["answers"]}

# v7 有 notices 与 coordinator_workdir，notices 还没有 target_session_id / wake_kind 两列。
V7_REQUIRED_TABLES = V6_REQUIRED_TABLES | {"notices"}
V7_REQUIRED_COLUMNS = {**V6_REQUIRED_COLUMNS,
                       "attempts": V3_ATTEMPT_COLUMNS | {"coordinator_session_id", "coordinator_workdir"},
                       "notices": NOTICES_BEFORE_MESSAGE_ID - {"target_session_id", "wake_kind"}}

# v8 有 notices 的两列，还没有 submissions.head_sha 与 merges.reviewed_sha。
V8_REQUIRED_TABLES = V7_REQUIRED_TABLES
# 13 之前 submissions 的列：冻结形状从这里减，不从 REQUIRED_COLUMNS 直接减——
# 后者会把以后新增的列自动算进旧版本应有的形状里（`conflict_preview` 刚踩过这一下）。
SUBMISSIONS_BEFORE_PREVIEW = REQUIRED_COLUMNS["submissions"] - {"conflict_preview"}
V8_REQUIRED_COLUMNS = {**V7_REQUIRED_COLUMNS,
                       "submissions": SUBMISSIONS_BEFORE_PREVIEW - {"head_sha", "base_sha"}}

# v9 有 head_sha 与 reviewed_sha，还没有 criteria_version 与 base_sha。
V9_REQUIRED_TABLES = V8_REQUIRED_TABLES
V9_REQUIRED_COLUMNS = {**V8_REQUIRED_COLUMNS, "submissions": SUBMISSIONS_BEFORE_PREVIEW - {"base_sha"}}

# v10 有 criteria_version 与 base_sha，还没有 ticket_links 与 projects.next_number；
# 它是最后一版「tickets.prd_sha256 是 NOT NULL」的形状，所以升级要重建 tickets 表。
V10_REQUIRED_TABLES = V9_REQUIRED_TABLES
V10_REQUIRED_COLUMNS = {**V9_REQUIRED_COLUMNS, "tickets": V3_TICKET_COLUMNS | {"criteria_version"},
                        "projects": {"id", "name", "root"}}

# v11 有发号盘与依赖边，还没有批次表、`tickets.batch_id` 与 `attempts.batch_key`；
# 它的 `active_session` 仍只按会话收窄（没有 `batch_key IS NULL` 那一半）。
V11_REQUIRED_TABLES = V10_REQUIRED_TABLES | {"ticket_links"}
V11_TICKET_COLUMNS = V3_TICKET_COLUMNS | {"criteria_version"}
V11_ATTEMPT_COLUMNS = V3_ATTEMPT_COLUMNS | {"coordinator_session_id", "coordinator_workdir", "criteria_version"}
V11_REQUIRED_COLUMNS = {**V10_REQUIRED_COLUMNS,
                        "tickets": V11_TICKET_COLUMNS,
                        "attempts": V11_ATTEMPT_COLUMNS,
                        "projects": {"id", "name", "root", "next_number"},
                        "ticket_links": REQUIRED_COLUMNS["ticket_links"]}

# v12 有车次表与两个指针列，还没有 `submissions.conflict_preview`——13 要加的正是那一列。
V12_REQUIRED_TABLES = V11_REQUIRED_TABLES | {"batches"}
V12_TICKET_COLUMNS = V11_TICKET_COLUMNS | {"batch_id"}
V12_ATTEMPT_COLUMNS = V11_ATTEMPT_COLUMNS | {"batch_key"}
V12_REQUIRED_COLUMNS = {**V11_REQUIRED_COLUMNS,
                        "tickets": V12_TICKET_COLUMNS,
                        "attempts": V12_ATTEMPT_COLUMNS,
                        "batches": REQUIRED_COLUMNS["batches"]}

# v13 有 `submissions.conflict_preview`，还没有 `tickets.requester_session_id` / `notices.message_id`
# 与 `tickets.owner_session_id`——14 加 requester 与 message_id（AW-106 先落地），15 加 owner（AW-114）。
# submissions 从这里减，不从 REQUIRED_COLUMNS 直接拿：后者会把以后新增的列自动算进 13 应有的
# 形状里，升级链一加级就拦下本该放行的旧库。
SUBMISSIONS_WITH_PREVIEW = SUBMISSIONS_BEFORE_PREVIEW | {"conflict_preview"}
V13_REQUIRED_TABLES = V12_REQUIRED_TABLES
V13_REQUIRED_COLUMNS = {**V12_REQUIRED_COLUMNS,
                        "tickets": V12_TICKET_COLUMNS,
                        "attempts": V12_ATTEMPT_COLUMNS,
                        "submissions": SUBMISSIONS_WITH_PREVIEW,
                        "notices": NOTICES_BEFORE_MESSAGE_ID}

# v14 是 AW-106 落地后的形状：多了 `tickets.requester_session_id` 与 `notices.message_id`，
# 还没有 `tickets.owner_session_id`——15 要加的正是那一列（AW-114 后落地，所以顺延一级）。
V14_REQUIRED_TABLES = V13_REQUIRED_TABLES
V14_TICKET_COLUMNS = V12_TICKET_COLUMNS | {"requester_session_id"}
V14_REQUIRED_COLUMNS = {**V13_REQUIRED_COLUMNS,
                        "tickets": V14_TICKET_COLUMNS,
                        "submissions": SUBMISSIONS_WITH_PREVIEW,
                        "notices": REQUIRED_COLUMNS["notices"]}

# v15 多出 `tickets.owner_session_id`；`merges` 仍是七列的老形状（16 才加 source 等四列），
# 这里显式冻结，不从 REQUIRED_COLUMNS 继承。
V15_REQUIRED_TABLES = V14_REQUIRED_TABLES
V15_TICKET_COLUMNS = V14_TICKET_COLUMNS | {"owner_session_id"}
V15_REQUIRED_COLUMNS = {**V14_REQUIRED_COLUMNS, "tickets": V15_TICKET_COLUMNS,
                        "merges": MERGES_BEFORE_SOURCE}


def column_names(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def column_notnull(conn, table, column):
    """这一列是否 NOT NULL；列不存在时返回 None（不是 False）。

    v11 要放宽的正是 `tickets.prd_sha256` 的 NOT NULL，而表名与列名在 v10 里已经全在——
    只看列集合分不出 10 与 11，所以形状守卫多看这一位。
    """
    for row in conn.execute(f"PRAGMA table_info({table})"):
        if row[1] == column:
            return bool(row[3])
    return None


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
    9: {
        "tables": V9_REQUIRED_TABLES,
        "columns": V9_REQUIRED_COLUMNS,
        "statements": [
            # 存量行填 1，不填 0：它们派发时确实只有一版判据，0 是一个谁都没用过的编号。
            "ALTER TABLE tickets ADD COLUMN criteria_version INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE attempts ADD COLUMN criteria_version INTEGER NOT NULL DEFAULT 1",
            # 老提交没有记过基座 sha，留 NULL 表示「那时没有这一项」。
            "ALTER TABLE submissions ADD COLUMN base_sha TEXT",
        ],
        "already_applied": lambda conn: ("criteria_version" in column_names(conn, "tickets")
                                         or "criteria_version" in column_names(conn, "attempts")
                                         or "base_sha" in column_names(conn, "submissions")),
        "already_applied_message": "Database claims version 9 but already has criteria_version or submissions.base_sha; inspect it, do not migrate",
    },
    10: {
        "tables": V10_REQUIRED_TABLES,
        "columns": V10_REQUIRED_COLUMNS,
        "statements": [
            # 发号盘：每个项目一条独立序列，存量项目下一个号由下面那条 UPDATE 按已有最大号算出。
            "ALTER TABLE projects ADD COLUMN next_number INTEGER NOT NULL DEFAULT 1",
            # prd_sha256 从 NOT NULL 改成可空，SQLite 只能重建表；逐列抄写、不补任何值——
            # 存量单的指纹原样保留，状态也原样保留（不回溯改成 planned）。
            "CREATE TABLE tickets_v11("
            " id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), title TEXT NOT NULL,"
            " deliverable TEXT NOT NULL, prd_path TEXT NOT NULL, prd_sha256 TEXT, criteria TEXT NOT NULL,"
            " revision INTEGER NOT NULL, criteria_version INTEGER NOT NULL DEFAULT 1,"
            " state TEXT NOT NULL, current_attempt TEXT, created_at TEXT NOT NULL)",
            "INSERT INTO tickets_v11(id,project_id,title,deliverable,prd_path,prd_sha256,criteria,revision,"
            "criteria_version,state,current_attempt,created_at)"
            " SELECT id,project_id,title,deliverable,prd_path,prd_sha256,criteria,revision,"
            "criteria_version,state,current_attempt,created_at FROM tickets",
            "DROP TABLE tickets",
            "ALTER TABLE tickets_v11 RENAME TO tickets",
            # 跟随 tickets 一起被 DROP 掉的索引要重建，否则按项目查工单会变成全表扫。
            "CREATE INDEX tickets_project ON tickets(project_id, created_at)",
            "CREATE TABLE ticket_links("
            " from_ticket TEXT NOT NULL REFERENCES tickets(id), kind TEXT NOT NULL,"
            " to_ticket TEXT NOT NULL REFERENCES tickets(id), created_at TEXT NOT NULL,"
            " PRIMARY KEY (from_ticket, kind, to_ticket),"
            " CHECK (kind IN ('blocked_by','related','rides_with')))",
            "CREATE INDEX ticket_links_to ON ticket_links(to_ticket, kind)",
            # 按已有最大号初始化（AW-096 -> 下一次 97）。历史号不改、不重排、不复用；
            # 带不出数字的 id（例如早期的 t1 之外的写法）不参与取最大，没有可用的就从 1 开始。
            "UPDATE projects SET next_number = COALESCE(("
            " SELECT MAX(CAST(substr(t.id, length(rtrim(t.id,'0123456789'))+1) AS INTEGER)) + 1"
            " FROM tickets t WHERE t.project_id = projects.id"
            "   AND rtrim(t.id,'0123456789') <> t.id"
            "   AND substr(t.id, length(rtrim(t.id,'0123456789'))+1) <> ''), 1)",
        ],
        "already_applied": lambda conn: ("next_number" in column_names(conn, "projects")
                                         or "ticket_links" in expected_tables(conn)
                                         or column_notnull(conn, "tickets", "prd_sha256") is False),
        "already_applied_message": "Database claims version 10 but already has projects.next_number, the ticket_links table or a nullable tickets.prd_sha256; inspect it, do not migrate",
    },
    11: {
        "tables": V11_REQUIRED_TABLES,
        "columns": V11_REQUIRED_COLUMNS,
        "statements": [
            # 车次表按 PRD-014 R4.1 的列建全；没有单独的「状态列」——open / closed 由 closed_at 一列表达，
            # 空即仍占用、非空即关闭，多一列 status 就多一处要与 closed_at 同步的真相。
            "CREATE TABLE batches("
            " id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),"
            " resource_key TEXT NOT NULL, owner_session_id TEXT,"
            " planned_at TEXT NOT NULL, closed_at TEXT)",
            # 存量单一个值都不写：batch_id / batch_key 留 NULL，所以升级后的行为与升级前逐字一致
            # （同一会话的第二张无车次单仍被 active_session 拒成 CONFLICT）。
            "ALTER TABLE tickets ADD COLUMN batch_id TEXT REFERENCES batches(id)",
            "ALTER TABLE attempts ADD COLUMN batch_key TEXT",
            # 索引收窄成只守无车次的那一半：车次内的多张单根本不在它的范围内。
            "DROP INDEX IF EXISTS active_session",
            "CREATE UNIQUE INDEX active_session ON attempts(session_id)"
            " WHERE batch_key IS NULL AND session_id IS NOT NULL AND state IN ('blocked','running','submitted')",
        ],
        # 声称 11 却已经有 12 的东西，或 tickets 还是 v10 的形状（prd_sha256 NOT NULL），一律拒。
        "already_applied": lambda conn: ("batches" in expected_tables(conn)
                                         or "batch_key" in column_names(conn, "attempts")
                                         or "batch_id" in column_names(conn, "tickets")
                                         or column_notnull(conn, "tickets", "prd_sha256") is not False),
        "already_applied_message": "Database claims version 11 but already has the batches table, attempts.batch_key or tickets.batch_id (or a v10-shaped tickets table); inspect it, do not migrate",
    },
    12: {
        "tables": V12_REQUIRED_TABLES,
        "columns": V12_REQUIRED_COLUMNS,
        "statements": [
            # 存量行留 NULL=「那时没有这一项」：补成空数组就等于声称历史提交都预演过且干净。
            # 可空正是这一列的语义：NULL 是「没跑过」，`{"conflicts":[]}` 才是「跑过且没撞」。
            "ALTER TABLE submissions ADD COLUMN conflict_preview TEXT",
        ],
        "already_applied": lambda conn: "conflict_preview" in column_names(conn, "submissions"),
        "already_applied_message": "Database claims version 12 but already has submissions.conflict_preview; inspect it, do not migrate",
    },
    13: {
        "tables": V13_REQUIRED_TABLES,
        "columns": V13_REQUIRED_COLUMNS,
        "statements": [
            # 两列都可空，存量行留 NULL=「那时没有这一项」。请求方不许拿协调会话冒充（PRD-015 R5），
            # 消息 id 也不许拿时间戳或标题凑一个：这两种都是把推定写成事实。
            "ALTER TABLE tickets ADD COLUMN requester_session_id TEXT",
            "ALTER TABLE notices ADD COLUMN message_id TEXT",
        ],
        "already_applied": lambda conn: ("requester_session_id" in column_names(conn, "tickets")
                                         or "message_id" in column_names(conn, "notices")
                                         or "owner_session_id" in column_names(conn, "tickets")),
        "already_applied_message": "Database claims version 13 but already has a 14 or 15 column; inspect it, do not migrate",
    },
    14: {
        "tables": V14_REQUIRED_TABLES,
        "columns": V14_REQUIRED_COLUMNS,
        "statements": [
            # 存量单的 owner 用「它当前那一代的 coordinator_session_id」回填：那是当时的责任人，
            # 不是「现在还活着的那条会话」。没有当前尝试的单留 NULL——给它们编一个 owner
            # 等于指名一条从没派发过这张单的会话。
            "ALTER TABLE tickets ADD COLUMN owner_session_id TEXT",
            "UPDATE tickets SET owner_session_id = ("
            " SELECT a.coordinator_session_id FROM attempts a WHERE a.id = tickets.current_attempt)"
            " WHERE owner_session_id IS NULL AND current_attempt IS NOT NULL",
        ],
        "already_applied": lambda conn: "owner_session_id" in column_names(conn, "tickets"),
        "already_applied_message": "Database claims version 14 but already has tickets.owner_session_id; inspect it, do not migrate",
    },
    15: {
        "tables": V15_REQUIRED_TABLES,
        "columns": V15_REQUIRED_COLUMNS,
        "statements": lambda conn: [
            f"ALTER TABLE merges ADD COLUMN {column} TEXT"
            for column in MERGE_BACKFILL_COLUMNS if column not in column_names(conn, "merges")
        ],
        # 四列全部可空，存量行留 NULL=「那时没有这一项」。批量填 `auto_merge` 或
        # `backfill` 会把「后来补的」和「当时记的」永久混在一起，正是这张单要分的东西。
        # 只拒绝「缺了一部分」：说明库被动过，不是这一级的正常中间态；四列都在、只有
        # 版本号没推进（真实发生过：另一个会话为了让 master 上的旧代码读库，把版本号
        # 单独改回 15）时应当把版本补完，而不是把人挡在门外。
        "already_applied": lambda conn: (
            any(column in column_names(conn, "merges") for column in MERGE_BACKFILL_COLUMNS)
            and not all(column in column_names(conn, "merges") for column in MERGE_BACKFILL_COLUMNS)),
        "already_applied_message": "Database claims version 15 but only some of merges.source / verified_by / verified_at / note exist; inspect it, do not migrate",
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
            # 逐级升级：3 → 4 → 5 → … → 11，每一级只跑自己那几条语句。
            current = version
            while current < SCHEMA_VERSION:
                step_statements = MIGRATIONS[current]["statements"]
                # 语句可以是固定列表，也可以按当前列集合现算（只用于「补完一半落地的迁移」
                # 那一级），调用处的形状一致。
                if callable(step_statements):
                    step_statements = step_statements(conn)
                for statement in step_statements:
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
