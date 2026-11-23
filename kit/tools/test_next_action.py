#!/usr/bin/env python
"""`tools/next_action.py` 的用例：四个结果各造一条、退出码三条分支各造一条。

判据只看两样东西：`scan()` 算出的 `action` 与 `main()` 的退出码。**不看实现细节**
（比如「有没有查过某张表」），因为那些会在下次重构时全变，而「有活 0 / 没活 2 /
出错 1」这三条是这个工具存在的全部理由。

库是临时 sqlite，只造工具真读的列。造表时把 `PRAGMA user_version` 写成
`task_center.db.SCHEMA_VERSION`——版本检查是 C4 的一部分，夹具里必须是一条
「版本对得上」的库，否则每个用例都会走错误分支。

时间戳一律写死成 `2026-11-22` 附近的固定值：这个工具没有时间阈值，但合并候选按
`created_at` 排序，写 now() 会让两条候选的先后随机。

跑法：

    python tools/test_next_action.py     # 单独跑，用例名逐条打印
    python tools/run_tests.py            # 随主线套件跑
"""
import contextlib
import datetime
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
if str(TOOLS.parent) not in sys.path:
    sys.path.insert(0, str(TOOLS.parent))

import next_action

from task_center.db import SCHEMA_VERSION

PROJECT = "ai-workspace"
CRITERIA_ONE = [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}]
SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "89abcdef0123456789abcdef0123456789abcdef"


def stamp(day: int) -> str:
    """固定的时间戳：2026-11-<day>T00:00:00+00:00。"""
    return f"2026-11-{day:02d}T00:00:00+00:00"


def create_db(path: Path, root: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, root TEXT);
        CREATE TABLE tickets (
            id TEXT PRIMARY KEY, project_id TEXT, title TEXT, deliverable TEXT,
            prd_path TEXT, prd_sha256 TEXT, criteria TEXT, revision INTEGER,
            criteria_version INTEGER, state TEXT, current_attempt TEXT,
            created_at TEXT, batch_id TEXT
        );
        CREATE TABLE attempts (
            id TEXT PRIMARY KEY, ticket_id TEXT, revision INTEGER, prd_path TEXT,
            prd_sha256 TEXT, state TEXT, session_id TEXT, created_at TEXT,
            coordinator_session_id TEXT, coordinator_workdir TEXT,
            criteria_version INTEGER, batch_key TEXT
        );
        CREATE TABLE submissions (
            id TEXT PRIMARY KEY, attempt_id TEXT, summary TEXT, artifacts TEXT,
            evidence TEXT, risks TEXT, created_at TEXT, head_sha TEXT,
            base_sha TEXT, conflict_preview TEXT
        );
        CREATE TABLE reviews (
            id TEXT PRIMARY KEY, submission_id TEXT, reviewer_session_id TEXT,
            verdict TEXT, created_at TEXT
        );
        CREATE TABLE merges (
            id TEXT PRIMARY KEY, ticket_id TEXT, commit_sha TEXT, target_branch TEXT,
            tests TEXT, created_at TEXT, reviewed_sha TEXT
        );
        CREATE TABLE notices (
            id TEXT PRIMARY KEY, ticket_id TEXT, status TEXT, detail TEXT,
            target_session_id TEXT, wake_kind TEXT, created_at TEXT
        );
        CREATE TABLE questions (
            id TEXT PRIMARY KEY, attempt_id TEXT, question TEXT, state TEXT,
            asked_at TEXT
        );
        CREATE TABLE ticket_links (
            from_ticket TEXT, kind TEXT, to_ticket TEXT, created_at TEXT
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, operation TEXT,
            detail TEXT, created_at TEXT
        );
        """
    )
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.execute(
        "INSERT INTO projects (id, name, root) VALUES (?,?,?)",
        (PROJECT, "AI工作区", root),
    )
    return conn


class Fixture(unittest.TestCase):
    """每个用例一条临时库 + 一个临时目录当项目根（用来验项目推断）。"""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.db_path = self.root / "task-center.sqlite3"
        self.conn = create_db(self.db_path, str(self.root))
        self.pause = self.root / "dispatch-paused"
        self.addCleanup(self._temp.cleanup)

    def tearDown(self):
        self.conn.close()

    # ---- 造数据

    def add_ticket(self, ticket_id, state="planned", created_day=20,
                   prd_path="PRD/PRD-014-计划队列与依赖.md", project_id=PROJECT,
                   criteria=CRITERIA_ONE):
        """默认给一条可判定条件：从 AW-109 起空条件的单不派（它是念头，不是可领的活）。"""
        self.conn.execute(
            "INSERT INTO tickets (id, project_id, title, deliverable, prd_path,"
            " prd_sha256, criteria, revision, criteria_version, state,"
            " current_attempt, created_at, batch_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticket_id, project_id, f"{ticket_id} 的标题", "交付物", prd_path,
             "a" * 64, json.dumps(criteria, ensure_ascii=False), 1, 1, state, None,
             stamp(created_day), None),
        )

    def add_attempt(self, ticket_id, attempt_id, state="running",
                    session_id="sess-1", created_day=20):
        self.conn.execute(
            "INSERT INTO attempts (id, ticket_id, revision, prd_path, prd_sha256,"
            " state, session_id, created_at, coordinator_session_id,"
            " coordinator_workdir, criteria_version, batch_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (attempt_id, ticket_id, 1, "PRD/PRD-014-计划队列与依赖.md", "a" * 64,
             state, session_id, stamp(created_day), "coord-1", str(self.root), 1,
             None),
        )
        self.conn.execute(
            "UPDATE tickets SET current_attempt=? WHERE id=?", (attempt_id, ticket_id)
        )

    def add_submission(self, attempt_id, submission_id, head_sha=SHA, created_day=20):
        self.conn.execute(
            "INSERT INTO submissions (id, attempt_id, summary, artifacts, evidence,"
            " risks, created_at, head_sha, base_sha, conflict_preview)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (submission_id, attempt_id, "做完了", "[]", "[]", "[]", stamp(created_day),
             head_sha, "b" * 40, None),
        )

    def add_review(self, submission_id, review_id="review-1"):
        self.conn.execute(
            "INSERT INTO reviews (id, submission_id, reviewer_session_id, verdict,"
            " created_at) VALUES (?,?,?,?,?)",
            (review_id, submission_id, "reviewer", "accept", stamp(21)),
        )

    def add_merge(self, ticket_id, commit="c" * 40):
        self.conn.execute(
            "INSERT INTO merges (id, ticket_id, commit_sha, target_branch, tests,"
            " created_at, reviewed_sha) VALUES (?,?,?,?,?,?,?)",
            (f"merge-{ticket_id}", ticket_id, commit, "master", "ok", stamp(21), SHA),
        )

    def add_notice(self, ticket_id, status="sent"):
        self.conn.execute(
            "INSERT INTO notices (id, ticket_id, status, detail, target_session_id,"
            " wake_kind, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"notice-{ticket_id}", ticket_id, status, "-", "coord-1", "unknown",
             stamp(21)),
        )

    def add_question(self, attempt_id, question_id="q-1"):
        self.conn.execute(
            "INSERT INTO questions (id, attempt_id, question, state, asked_at)"
            " VALUES (?,?,?,?,?)",
            (question_id, attempt_id, "选哪一条？", "open", stamp(20)),
        )

    def add_review_dispatched(self, ticket_id, submission_id, minutes_ago=5,
                              reviewer="reviewer-1"):
        """造一条「验收已经派出去」的事件。时间按「几分钟前」算，用例不用自己算 now。"""
        stamp = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(minutes=minutes_ago)).isoformat()
        detail = {"request_id": "req-review", "result": {
            "ticket_id": ticket_id, "submission_id": submission_id,
            "reviewer_session_id": reviewer, "note": None,
        }}
        self.conn.execute(
            "INSERT INTO events (ticket_id, operation, detail, created_at)"
            " VALUES (?,?,?,?)",
            (ticket_id, next_action.REVIEW_DISPATCHED,
             json.dumps(detail, ensure_ascii=False), stamp),
        )

    def add_events_table_row(self, ticket_id, operation, detail, created_at):
        self.conn.execute(
            "INSERT INTO events (ticket_id, operation, detail, created_at)"
            " VALUES (?,?,?,?)",
            (ticket_id, operation, json.dumps(detail, ensure_ascii=False), created_at),
        )

    def add_link(self, from_ticket, to_ticket, kind="blocked_by"):
        self.conn.execute(
            "INSERT INTO ticket_links (from_ticket, kind, to_ticket, created_at)"
            " VALUES (?,?,?,?)",
            (from_ticket, kind, to_ticket, stamp(19)),
        )

    # ---- 跑

    def run_main(self, *extra):
        self.conn.commit()
        return run_main([
            "--db", str(self.db_path),
            "--project", PROJECT,
            "--pause-file", str(self.pause),
            *extra,
        ])

    def reason(self, result):
        return result["reason_code"]


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = next_action.main(argv)
    return code, out.getvalue(), err.getvalue()


class InFlightTest(Fixture):
    """C1：有在途单挡住时退出 2，点名那张单与它停在哪一步，不给任何派发动作。"""

    def test_running_ticket_blocks_and_is_named(self):
        self.add_ticket("AW-901", state="running")
        self.add_attempt("AW-901", "a-901")
        self.add_ticket("AW-902", state="planned")
        code, out, _ = self.run_main()
        self.assertEqual(code, next_action.EXIT_IDLE)
        self.assertNotIn(next_action.DISPATCH_EXECUTE, out)
        self.assertIn("AW-901", out)
        self.assertIn("执行会话在跑", out)

    def test_running_ticket_json_carries_state_and_step(self):
        self.add_ticket("AW-903", state="running")
        self.add_attempt("AW-903", "a-903", session_id="sess-903")
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_IDLE)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.NOTHING)
        self.assertEqual(result["reason_code"], "in_flight")
        self.assertIsNone(result["dispatch"])
        self.assertEqual(result["in_flight"][0]["ticket_id"], "AW-903")
        self.assertEqual(result["in_flight"][0]["session_id"], "sess-903")
        self.assertEqual(result["candidates"][next_action.DISPATCH_EXECUTE], [])

    def test_dispatching_says_bind_session(self):
        self.add_ticket("AW-904", state="dispatching")
        self.add_attempt("AW-904", "a-904", state="dispatching", session_id=None)
        code, out, _ = self.run_main()
        self.assertEqual(code, next_action.EXIT_IDLE)
        self.assertIn("bind_session", out)

    def test_blocked_names_the_open_question(self):
        self.add_ticket("AW-905", state="blocked")
        self.add_attempt("AW-905", "a-905", state="blocked")
        self.add_question("a-905", "q-905")
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_IDLE)
        result = json.loads(out)
        self.assertEqual(result["in_flight"][0]["question_id"], "q-905")
        self.assertIn("等一句话", result["human"])


class ExecuteTest(Fixture):
    """C2：没在途单、池子里有可领单时退出 0，给 dispatch_execute + 单号 + PRD 依据。"""

    def test_claimable_planned_dispatches_execute(self):
        self.add_ticket("AW-910", state="planned")
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_WORK)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.DISPATCH_EXECUTE)
        self.assertEqual(result["dispatch"]["ticket_id"], "AW-910")
        self.assertEqual(result["dispatch"]["prd_path"],
                         "PRD/PRD-014-计划队列与依赖.md")
        self.assertIn("dispatch_execute", result["human"])
        self.assertIn("AW-910", result["human"])

    def test_ready_and_rework_are_claimable(self):
        self.add_ticket("AW-911", state="ready")
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_WORK)
        self.assertEqual(json.loads(out)["dispatch"]["ticket_id"], "AW-911")

        self.conn.execute("UPDATE tickets SET state='rework' WHERE id='AW-911'")
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_WORK)
        self.assertEqual(json.loads(out)["dispatch"]["ticket_id"], "AW-911")

    def test_pending_blocker_keeps_ticket_out_of_the_pool(self):
        self.add_ticket("AW-912", state="planned", created_day=18)
        self.add_ticket("AW-913", state="planned", created_day=19)
        self.add_link("AW-912", "AW-913")
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_WORK)
        result = json.loads(out)
        self.assertEqual(result["dispatch"]["ticket_id"], "AW-913")
        self.assertNotIn("AW-912", result["candidates"][next_action.DISPATCH_EXECUTE])
        pending = [item for item in result["skipped"]
                   if item["reason"] == "blocked_by_pending"]
        self.assertTrue(pending)
        self.assertEqual(pending[0]["tickets"][0]["ticket_id"], "AW-912")
        self.assertEqual(pending[0]["tickets"][0]["blocked_by"][0]["ticket_id"], "AW-913")

        # 前置 done 之后，下游按依赖层数排在后面，但不是不可领。
        self.conn.execute("UPDATE tickets SET state='done' WHERE id='AW-913'")
        code, out, _ = self.run_main("--json")
        result = json.loads(out)
        self.assertEqual(result["dispatch"]["ticket_id"], "AW-912")
        self.assertEqual(result["dispatch"]["depth"], 1)

    def test_ticket_without_criteria_is_not_dispatched(self):
        """空条件的 planned 是池子里的念头：不派出去，进 skipped 并说明原因（AW-109）。"""
        self.add_ticket("AW-914", state="planned", criteria=[])
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_IDLE)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.NOTHING)
        skipped = [item for item in result["skipped"] if item["reason"] == "no_criteria"]
        self.assertTrue(skipped, result)
        self.assertEqual(skipped[0]["tickets"][0]["ticket_id"], "AW-914")

    def test_other_project_tickets_are_not_candidates(self):
        self.add_ticket("OTHER-1", state="planned", project_id="another-project")
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_IDLE)
        self.assertEqual(json.loads(out)["action"], next_action.NOTHING)


class ReviewTest(Fixture):
    """C3 前一半：有等待验收的单时给 dispatch_review，并带 submission_id 与 head_sha。"""

    def test_awaiting_review_dispatches_review(self):
        self.add_ticket("AW-920", state="awaiting_review")
        self.add_attempt("AW-920", "a-920", state="submitted")
        self.add_submission("a-920", "sub-920")
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_WORK)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.DISPATCH_REVIEW)
        self.assertEqual(result["dispatch"]["submission_id"], "sub-920")
        self.assertEqual(result["dispatch"]["head_sha"], SHA)

    def test_awaiting_review_with_review_row_stops_instead_of_redispatching(self):
        self.add_ticket("AW-921", state="awaiting_review")
        self.add_attempt("AW-921", "a-921", state="submitted")
        self.add_submission("a-921", "sub-921")
        self.add_review("sub-921")
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_IDLE)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.NOTHING)
        self.assertEqual(result["reason_code"], "needs_human")
        self.assertTrue(result["stop"])

    def test_awaiting_review_without_submission_stops(self):
        self.add_ticket("AW-922", state="awaiting_review")
        self.add_attempt("AW-922", "a-922", state="submitted")
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_IDLE)
        result = json.loads(out)
        self.assertEqual(result["reason_code"], "needs_human")
        self.assertIn("AW-922", " ".join(result["stop"]))


class ReviewInFlightTest(Fixture):
    """AW-133：验收已经派出去、结论还没落库时，不许再派第二个验收人。

    要判的是三件事：窗口内不报 `dispatch_review`、窗口过了自动回落、以及这条事实
    **不挡**另外三档（C3 的反例）。
    """

    def review_ticket(self, ticket_id="AW-925", created_day=18, submission_id="sub-925"):
        self.add_ticket(ticket_id, state="awaiting_review", created_day=created_day)
        self.add_attempt(ticket_id, f"a-{ticket_id}", state="submitted")
        self.add_submission(f"a-{ticket_id}", submission_id)
        return submission_id

    def test_review_in_flight_is_not_dispatched_again(self):
        submission = self.review_ticket()
        self.add_review_dispatched("AW-925", submission)
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_IDLE)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.NOTHING)
        self.assertEqual(result["reason_code"], "review_in_flight")
        self.assertEqual([], result["candidates"][next_action.DISPATCH_REVIEW])
        self.assertEqual(result["review_in_flight"][0]["ticket_id"], "AW-925")
        self.assertEqual(result["review_in_flight"][0]["submission_id"], submission)
        self.assertIn("30 分钟", result["human"])

    def test_review_falls_back_after_the_window(self):
        """31 分钟还没有 reviews 行：验收会话大概根本没起来，回落成可派。"""
        submission = self.review_ticket()
        self.add_review_dispatched("AW-925", submission, minutes_ago=31)
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_WORK)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.DISPATCH_REVIEW)
        self.assertEqual(result["dispatch"]["ticket_id"], "AW-925")
        self.assertEqual(result["review_in_flight"], [])

    def test_in_flight_review_does_not_block_a_merge(self):
        """C3 的反例：池子里另有已验收待合的单时，输出仍是 merge，不被在飞验收挡住。"""
        submission = self.review_ticket()
        self.add_review_dispatched("AW-925", submission)
        self.add_ticket("AW-926", state="done", created_day=19)
        self.add_attempt("AW-926", "a-AW-926", state="accepted")
        self.add_submission("a-AW-926", "sub-926", created_day=19)
        self.add_notice("AW-926")
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_WORK)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.MERGE)
        self.assertEqual(result["dispatch"]["ticket_id"], "AW-926")
        self.assertEqual(result["review_in_flight"][0]["ticket_id"], "AW-925")

    def test_in_flight_review_does_not_block_dispatch_execute(self):
        """可领单照旧给 dispatch_execute：`review_dispatched` 不进 BLOCKING_STATES。"""
        submission = self.review_ticket()
        self.add_review_dispatched("AW-925", submission)
        self.add_ticket("AW-927", state="planned")
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_WORK)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.DISPATCH_EXECUTE)
        self.assertEqual(result["dispatch"]["ticket_id"], "AW-927")

    def test_event_for_an_older_submission_does_not_suppress(self):
        """返工后的新提交还没人看过：指向旧提交的事件不能压住这张单。"""
        submission = self.review_ticket()
        self.add_review_dispatched("AW-925", submission, minutes_ago=10)
        self.add_submission("a-AW-925", "sub-925-r2", created_day=21)
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_WORK)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.DISPATCH_REVIEW)
        self.assertEqual(result["dispatch"]["submission_id"], "sub-925-r2")

    def test_rejection_row_does_not_count_as_dispatched(self):
        """被拒的调用也写 events，但它带 `rejected` 而不是 `result`：那不是「已派」。"""
        self.review_ticket()
        self.add_events_table_row(
            "AW-925", next_action.REVIEW_DISPATCHED,
            {"rejected": "ALREADY_REVIEWED", "request_id": "req-x"},
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_WORK)
        self.assertEqual(json.loads(out)["action"], next_action.DISPATCH_REVIEW)

    def test_review_row_still_stops_instead_of_waiting(self):
        """已有验收结论是状态机慢一步，不是「在飞」：照旧进 stop 等人看。"""
        submission = self.review_ticket()
        self.add_review_dispatched("AW-925", submission)
        self.add_review(submission)
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_IDLE)
        result = json.loads(out)
        self.assertEqual(result["reason_code"], "needs_human")
        self.assertTrue(result["stop"])


class MergeTest(Fixture):
    """C3 后一半：done 且无合并记录时给 merge；有 merges 行就不再给。"""

    def test_done_without_merge_gives_merge(self):
        self.add_ticket("AW-930", state="done")
        self.add_attempt("AW-930", "a-930", state="accepted")
        self.add_submission("a-930", "sub-930")
        self.add_notice("AW-930")
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_WORK)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.MERGE)
        self.assertEqual(result["dispatch"]["ticket_id"], "AW-930")
        self.assertEqual(result["dispatch"]["head_sha"], SHA)

    def test_merged_ticket_is_not_a_candidate(self):
        self.add_ticket("AW-931", state="done")
        self.add_attempt("AW-931", "a-931", state="accepted")
        self.add_submission("a-931", "sub-931")
        self.add_merge("AW-931")
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_IDLE)
        self.assertEqual(json.loads(out)["action"], next_action.NOTHING)

    def test_missing_notice_adds_a_stop_hint(self):
        self.add_ticket("AW-932", state="done")
        self.add_attempt("AW-932", "a-932", state="accepted")
        self.add_submission("a-932", "sub-932")
        code, out, _ = self.run_main("--json")
        result = json.loads(out)
        self.assertEqual(code, next_action.EXIT_WORK)
        self.assertEqual(result["action"], next_action.MERGE)
        self.assertIn("record_notice", " ".join(result["stop"]))

    def test_no_branch_head_is_named_in_the_human_line_too(self):
        """不派 merge，但人读那一行也要说明白：这类单不会被悄悄丢掉。"""
        self.add_ticket("AW-935", state="done")
        self.add_attempt("AW-935", "a-935", state="accepted")
        self.add_submission("a-935", "sub-935", head_sha=None)
        code, out, _ = self.run_main()
        self.assertEqual(code, next_action.EXIT_IDLE)
        self.assertIn("AW-935", out)
        self.assertIn("没有分支头", out)

    def test_no_branch_head_is_skipped_not_merged(self):
        """合并流程之前的直接提交：库里没有可合的树，进 skipped 而不是派 merge。"""
        self.add_ticket("AW-933", state="done")
        self.add_attempt("AW-933", "a-933", state="accepted")
        self.add_submission("a-933", "sub-933", head_sha=None)
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_IDLE)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.NOTHING)
        self.assertEqual(result["skipped"][0]["reason"], "no_branch_head")
        self.assertEqual(result["skipped"][0]["ticket_ids"], ["AW-933"])

    def test_done_without_any_submission_stops(self):
        self.add_ticket("AW-934", state="done")
        self.add_attempt("AW-934", "a-934", state="accepted")
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_IDLE)
        result = json.loads(out)
        self.assertEqual(result["reason_code"], "needs_human")
        self.assertIn("AW-934", " ".join(result["stop"]))


class ExclusivityTest(Fixture):
    """C3：三种派发动作互斥，一次只出现一个。"""

    def test_review_wins_and_other_candidates_stay_candidates(self):
        self.add_ticket("AW-940", state="awaiting_review", created_day=18)
        self.add_attempt("AW-940", "a-940", state="submitted")
        self.add_submission("a-940", "sub-940")
        self.add_ticket("AW-941", state="done", created_day=19)
        self.add_attempt("AW-941", "a-941", state="accepted")
        self.add_submission("a-941", "sub-941", created_day=19)
        self.add_ticket("AW-942", state="planned", created_day=20)

        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_WORK)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.DISPATCH_REVIEW)
        self.assertEqual(result["dispatch"]["ticket_id"], "AW-940")
        self.assertEqual(result["candidates"][next_action.MERGE], ["AW-941"])
        self.assertEqual(result["candidates"][next_action.DISPATCH_EXECUTE], ["AW-942"])
        self.assertNotIn("AW-941", json.dumps(result["dispatch"]))
        self.assertNotIn("AW-942", json.dumps(result["dispatch"]))

    def test_merge_beats_execute(self):
        self.add_ticket("AW-943", state="done", created_day=19)
        self.add_attempt("AW-943", "a-943", state="accepted")
        self.add_submission("a-943", "sub-943", created_day=19)
        self.add_ticket("AW-944", state="planned", created_day=20)
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_WORK)
        self.assertEqual(json.loads(out)["action"], next_action.MERGE)


class ErrorTest(Fixture):
    """C4：读不了库、版本不对、缺表都退 1，不许退化成「没活」的 2。"""

    def test_missing_db_exits_one(self):
        missing = self.root / "nope.sqlite3"
        code, out, err = run_main(["--db", str(missing), "--project", PROJECT])
        self.assertEqual(code, next_action.EXIT_ERROR)
        self.assertIn("DB_NOT_FOUND", err)
        self.assertEqual(out.strip(), "")

    def test_wrong_schema_version_exits_one(self):
        self.add_ticket("AW-950", state="planned")
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION - 1}")
        code, out, err = self.run_main()
        self.assertEqual(code, next_action.EXIT_ERROR)
        self.assertIn("SCHEMA_MISMATCH", err)
        self.assertEqual(out.strip(), "")

    def test_missing_table_exits_one(self):
        self.add_ticket("AW-951", state="planned")
        self.conn.execute("DROP TABLE merges")
        code, _, err = self.run_main()
        self.assertEqual(code, next_action.EXIT_ERROR)
        self.assertIn("SCHEMA_MISMATCH", err)

    def test_not_a_database_exits_one(self):
        broken = self.root / "broken.sqlite3"
        broken.write_text("这不是一个 sqlite 文件", encoding="utf-8")
        code, _, err = run_main(["--db", str(broken), "--project", PROJECT])
        self.assertEqual(code, next_action.EXIT_ERROR)
        self.assertIn("DB_UNREADABLE", err)

    def test_unknown_project_exits_one(self):
        code, _, err = run_main(["--db", str(self.db_path), "--project", "no-such"])
        self.assertEqual(code, next_action.EXIT_ERROR)
        self.assertIn("PROJECT_NOT_FOUND", err)

    def test_bad_arguments_exit_one_not_two(self):
        """参数写错也是 1：错参数是「不知道」，不是「没活」。"""
        code, _, _ = run_main(["--no-such-flag"])
        self.assertEqual(code, next_action.EXIT_ERROR)


class ProjectTest(Fixture):
    """项目归属：能按目录推断，推断不出就报错，不猜。"""

    def test_inferred_from_cwd(self):
        self.add_ticket("AW-960", state="planned")
        self.conn.commit()
        cwd = Path.cwd()
        try:
            import os
            os.chdir(self.root)
            result = next_action.scan(
                self.conn, None, pause_path=self.pause, cwd=self.root
            )
        finally:
            os.chdir(cwd)
        self.assertEqual(result["project_id"], PROJECT)
        self.assertEqual(result["action"], next_action.DISPATCH_EXECUTE)

    def test_outside_any_project_exits_one(self):
        # 必须挑一个真的不在项目 root 底下的目录：本夹具的 root 是临时目录，
        # 在它下面再建一个子目录仍然是「在项目里」。
        with tempfile.TemporaryDirectory() as elsewhere:
            self.conn.commit()
            with self.assertRaises(next_action.PlannerError):
                next_action.scan(self.conn, None, pause_path=self.pause,
                                 cwd=Path(elsewhere))


class PauseTest(Fixture):
    """暂停：标记存在就退出 2 并说明暂停中，一个动作都不给。"""

    def test_pause_marker_silences_the_loop(self):
        self.add_ticket("AW-970", state="planned")
        self.pause.write_text("", encoding="utf-8")
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_IDLE)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.NOTHING)
        self.assertEqual(result["reason_code"], "paused")
        self.assertIn("暂停中", result["human"])

    def test_no_pause_marker_runs_normally(self):
        self.add_ticket("AW-971", state="planned")
        code, _, _ = self.run_main()
        self.assertEqual(code, next_action.EXIT_WORK)


class OutputTest(Fixture):
    """两种输出都要能读：人读一句、机器读一份带 human 的 JSON。"""

    def test_human_output_is_one_line(self):
        self.add_ticket("AW-980", state="planned")
        code, out, _ = self.run_main()
        self.assertEqual(code, next_action.EXIT_WORK)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertTrue(out.strip().startswith(next_action.DISPATCH_EXECUTE))

    def test_json_carries_human_and_project(self):
        self.add_ticket("AW-981", state="planned")
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_WORK)
        result = json.loads(out)
        self.assertEqual(result["project_id"], PROJECT)
        self.assertIn("human", result)
        self.assertEqual(result["schema"], 1)
        self.assertEqual(result["action"], next_action.DISPATCH_EXECUTE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
