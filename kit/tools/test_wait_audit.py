#!/usr/bin/env python
"""`tools/wait_audit.py` 的用例：五类各造一条命中 + 一条不命中，再验退出码。

为什么要固定时间戳：这个工具唯一的产出是「等了多久 / 停了多久」，拿 now() 造数据
就等于把用例和实现绑在一起，跑两次结果还不一样。所以 `scan()` 的用例先钉一个 `NOW`，
把库里的时间写成 `NOW - N 分钟`。

只有退出码那几个用例必须用真实时钟（`main()` 内部取 `now()`），它们按「相对现在
几小时前」造数据，仍然不依赖具体日期。

分工：库是临时 sqlite，只造工具真读的列，不为它没读的列造列。判据只看 `scan()`
的结果与退出码，不看实现细节。

跑法：

    python tools/test_wait_audit.py      # 单独跑，用例名逐条打印
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wait_audit

# 夹具里的「现在」：固定值，避免用例随真实时间漂移。
NOW = datetime.datetime(2026, 11, 22, 12, 0, tzinfo=datetime.timezone.utc)


def iso(minutes_ago: float, now: datetime.datetime = NOW) -> str:
    return (now - datetime.timedelta(minutes=minutes_ago)).isoformat()


def create_ticket_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE tickets (
            id TEXT PRIMARY KEY, project_id TEXT, title TEXT, state TEXT
        );
        CREATE TABLE attempts (
            id TEXT PRIMARY KEY, ticket_id TEXT, state TEXT, session_id TEXT
        );
        CREATE TABLE submissions (
            id TEXT PRIMARY KEY, attempt_id TEXT, created_at TEXT
        );
        CREATE TABLE reviews (
            id TEXT PRIMARY KEY, submission_id TEXT DEFAULT NULL,
            reviewer_session_id TEXT, created_at TEXT
        );
        CREATE TABLE notices (
            id TEXT PRIMARY KEY, ticket_id TEXT, status TEXT
        );
        CREATE TABLE merges (
            id TEXT PRIMARY KEY, ticket_id TEXT, commit_sha TEXT
        );
        """
    )
    return conn


def create_sessions_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, title TEXT, last_activity_at TEXT,
            active_turn_started_at TEXT, last_turn_ended_at TEXT
        );
        """
    )
    return conn


def add_ticket(conn, ticket_id, state, title="一张单"):
    conn.execute(
        "INSERT INTO tickets (id, project_id, title, state) VALUES (?,?,?,?)",
        (ticket_id, "ai-workspace", title, state),
    )


def add_attempt(conn, ticket_id, attempt_id, state="running", session_id=None):
    conn.execute(
        "INSERT INTO attempts (id, ticket_id, state, session_id) VALUES (?,?,?,?)",
        (attempt_id, ticket_id, state, session_id),
    )


def add_submission(conn, attempt_id, submission_id, minutes_ago):
    conn.execute(
        "INSERT INTO submissions (id, attempt_id, created_at) VALUES (?,?,?)",
        (submission_id, attempt_id, iso(minutes_ago)),
    )


def add_review(conn, submission_id, review_id="review-1"):
    conn.execute(
        "INSERT INTO reviews (id, submission_id, reviewer_session_id, created_at)"
        " VALUES (?,?,?,?)",
        (review_id, submission_id, "reviewer", iso(10)),
    )


def add_session(conn, session_id, minutes_ago=None, turn_active=False, title="会话"):
    started = iso(minutes_ago or 0) if turn_active else None
    conn.execute(
        "INSERT INTO sessions (id, title, last_activity_at, active_turn_started_at,"
        " last_turn_ended_at) VALUES (?,?,?,?,?)",
        (session_id, title, None if minutes_ago is None else iso(minutes_ago),
         started, None),
    )


def add_notice(conn, ticket_id, notice_id="notice-1", status="sent"):
    """造一行回执记录。`failed` 也算走过回执，与 `merge_ticket` 的 `NO_NOTICE` 同一把尺。"""
    conn.execute(
        "INSERT INTO notices (id, ticket_id, status) VALUES (?,?,?)",
        (notice_id, ticket_id, status),
    )


class TempDbCase(unittest.TestCase):
    """每个用例一组临时库，用完就删。"""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.ticket_path = self.root / "task-center.sqlite3"
        self.sessions_path = self.root / "cindy-test.db"
        self.ticket = create_ticket_db(self.ticket_path)
        self.sessions = create_sessions_db(self.sessions_path)
        self.addCleanup(self._temp.cleanup)

    def tearDown(self):
        self.ticket.close()
        self.sessions.close()

    def run_scan(self, idle_minutes=wait_audit.SESSION_IDLE_MINUTES):
        self.ticket.commit()
        self.sessions.commit()
        return wait_audit.scan(
            self.ticket, self.sessions, now=NOW, idle_minutes=idle_minutes
        )

    def kinds(self, result):
        return [hit["kind"] for hit in result["hits"]]


class WaitingReviewTest(TempDbCase):
    """S3：4 小时且没有 review 行才报；有 review 行、或没到 4 小时都不报。

    三条夹具都先放一行回执（`notices`）——不然 S3a 那档（待验收、提交超过 20 分钟、
    notices 一行都没有）也会命中同一张单，这组用例就测不出 S3 自己的边界了。
    """

    def test_hit_after_four_hours_without_review(self):
        add_ticket(self.ticket, "AW-901", "awaiting_review")
        add_attempt(self.ticket, "AW-901", "a-901", state="submitted")
        add_submission(self.ticket, "a-901", "s-901", minutes_ago=5 * 60)
        add_notice(self.ticket, "AW-901")
        result = self.run_scan()
        self.assertEqual(self.kinds(result), [wait_audit.WAITING_REVIEW])
        self.assertEqual(result["summary"][wait_audit.WAITING_REVIEW], 1)
        self.assertEqual(result["hits"][0]["ticket_id"], "AW-901")

    def test_not_hit_before_four_hours(self):
        add_ticket(self.ticket, "AW-902", "awaiting_review")
        add_attempt(self.ticket, "AW-902", "a-902", state="submitted")
        add_submission(self.ticket, "a-902", "s-902", minutes_ago=3 * 60 + 59)
        add_notice(self.ticket, "AW-902")
        result = self.run_scan()
        self.assertEqual(result["hits"], [])

    def test_not_hit_when_review_row_exists(self):
        add_ticket(self.ticket, "AW-903", "awaiting_review")
        add_attempt(self.ticket, "AW-903", "a-903", state="submitted")
        add_submission(self.ticket, "a-903", "s-903", minutes_ago=9 * 60)
        add_notice(self.ticket, "AW-903")
        add_review(self.ticket, "s-903")
        result = self.run_scan()
        self.assertEqual(result["hits"], [])
        reasons = [item["reason"] for item in result["ignored"]]
        self.assertIn("already_reviewed", reasons)


class AwaitingReviewNoNoticeTest(TempDbCase):
    """S3a 的 20 分钟：交完两轮巡检还没人接、回执也一行没有才报。

    这一档是 4 小时那档的分钟级前哨，所以两条用例都不该碰到 `waiting_review`：
    21 分钟与 5 分钟都远在 4 小时以内。
    """

    def test_hit_at_21_minutes_without_any_notice(self):
        add_ticket(self.ticket, "AW-905", "awaiting_review")
        add_attempt(self.ticket, "AW-905", "a-905", state="submitted")
        add_submission(self.ticket, "a-905", "s-905", minutes_ago=21)
        result = self.run_scan()
        self.assertEqual(self.kinds(result), [wait_audit.AWAITING_REVIEW_NO_NOTICE])
        self.assertEqual(result["summary"][wait_audit.AWAITING_REVIEW_NO_NOTICE], 1)
        self.assertEqual(result["hits"][0]["ticket_id"], "AW-905")

    def test_not_hit_at_5_minutes(self):
        add_ticket(self.ticket, "AW-906", "awaiting_review")
        add_attempt(self.ticket, "AW-906", "a-906", state="submitted")
        add_submission(self.ticket, "a-906", "s-906", minutes_ago=5)
        result = self.run_scan()
        self.assertEqual(result["hits"], [])

    def test_not_hit_when_any_notice_row_exists(self):
        """`notices` 判的是「这张单一行都没有」，不是「最新提交之后没有」。"""
        add_ticket(self.ticket, "AW-907", "awaiting_review")
        add_attempt(self.ticket, "AW-907", "a-907", state="submitted")
        add_submission(self.ticket, "a-907", "s-907", minutes_ago=90)
        self.ticket.execute(
            "INSERT INTO notices (id, ticket_id, status) VALUES (?,?,?)",
            ("n-907", "AW-907", "failed"),
        )
        result = self.run_scan()
        self.assertEqual(result["hits"], [])


class DoneTest(TempDbCase):
    """done 的两类各自独立：有回执不等于有合并记录，反之亦然。"""

    def test_done_without_notice_and_without_merge(self):
        add_ticket(self.ticket, "AW-910", "done")
        result = self.run_scan()
        self.assertEqual(
            sorted(self.kinds(result)),
            sorted([wait_audit.DONE_NO_NOTICE, wait_audit.DONE_NOT_MERGED]),
        )

    def test_done_with_notice_and_merge_is_clean(self):
        add_ticket(self.ticket, "AW-911", "done")
        self.ticket.execute(
            "INSERT INTO notices (id, ticket_id, status) VALUES (?,?,?)",
            ("n-911", "AW-911", "sent"),
        )
        self.ticket.execute(
            "INSERT INTO merges (id, ticket_id, commit_sha) VALUES (?,?,?)",
            ("m-911", "AW-911", "0" * 40),
        )
        result = self.run_scan()
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["summary"], {kind: 0 for kind in wait_audit.KINDS})

    def test_done_with_notice_but_no_merge(self):
        add_ticket(self.ticket, "AW-912", "done")
        self.ticket.execute(
            "INSERT INTO notices (id, ticket_id, status) VALUES (?,?,?)",
            ("n-912", "AW-912", "sent"),
        )
        result = self.run_scan()
        self.assertEqual(self.kinds(result), [wait_audit.DONE_NOT_MERGED])


class RunningIdleTest(TempDbCase):
    """S4 的 10 分钟：只报「单在跑、会话静了」；在跑这一轮、没到 10 分钟的都不报。"""

    def test_hit_when_session_idle(self):
        add_ticket(self.ticket, "AW-920", "running")
        add_attempt(self.ticket, "AW-920", "a-920", state="running",
                    session_id="sess-920")
        add_session(self.sessions, "sess-920", minutes_ago=30)
        result = self.run_scan()
        self.assertEqual(self.kinds(result), [wait_audit.RUNNING_IDLE])
        self.assertEqual(result["hits"][0]["session_id"], "sess-920")

    def test_not_hit_within_ten_minutes(self):
        add_ticket(self.ticket, "AW-921", "running")
        add_attempt(self.ticket, "AW-921", "a-921", state="running",
                    session_id="sess-921")
        add_session(self.sessions, "sess-921", minutes_ago=9)
        result = self.run_scan()
        self.assertEqual(result["hits"], [])

    def test_not_hit_when_turn_is_active(self):
        add_ticket(self.ticket, "AW-922", "running")
        add_attempt(self.ticket, "AW-922", "a-922", state="running",
                    session_id="sess-922")
        add_session(self.sessions, "sess-922", minutes_ago=40, turn_active=True)
        result = self.run_scan()
        self.assertEqual(result["hits"], [])
        reasons = [item["reason"] for item in result["ignored"]]
        self.assertIn("turn_active", reasons)

    def test_not_hit_when_attempt_is_not_running(self):
        add_ticket(self.ticket, "AW-923", "running")
        add_attempt(self.ticket, "AW-923", "a-923", state="accepted",
                    session_id="sess-923")
        add_session(self.sessions, "sess-923", minutes_ago=120)
        result = self.run_scan()
        self.assertNotIn(wait_audit.RUNNING_IDLE, self.kinds(result))


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = wait_audit.main(argv)
    return code, out.getvalue(), err.getvalue()


class EntryPointTest(TempDbCase):
    """退出码与输出形状：末行固定五类合计，缺库必须是 2 而不是 0。"""

    def test_exit_zero_and_summary_line_when_empty(self):
        code, out, err = run_main([
            "--db", str(self.ticket_path),
            "--sessions-db", str(self.sessions_path),
            "--minutes", "10",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        last = out.strip().splitlines()[-1]
        self.assertTrue(last.startswith("合计："))
        for label in wait_audit.KIND_LABELS.values():
            self.assertIn(label, last)
        self.assertIn("0 条", last)

    def test_exit_one_when_hit(self):
        add_ticket(self.ticket, "AW-930", "done")
        self.ticket.commit()
        code, out, _ = run_main([
            "--db", str(self.ticket_path),
            "--sessions-db", str(self.sessions_path),
        ])
        self.assertEqual(code, 1)
        self.assertIn(wait_audit.DONE_NO_NOTICE, out)
        self.assertIn("done 没回执 1 条", out.strip().splitlines()[-1])

    def test_missing_db_exits_two_with_db_not_found(self):
        code, _, err = run_main([
            "--db", str(self.root / "没有这个库.sqlite3"),
            "--sessions-db", str(self.sessions_path),
        ])
        self.assertEqual(code, 2)
        self.assertIn("DB_NOT_FOUND", err)

    def test_missing_sessions_db_exits_two_with_db_not_found(self):
        code, _, err = run_main([
            "--db", str(self.ticket_path),
            "--sessions-db", str(self.root / "没有这个会话库.db"),
        ])
        self.assertEqual(code, 2)
        self.assertIn("DB_NOT_FOUND", err)

    def test_json_output_has_original_four_counts(self):
        add_ticket(self.ticket, "AW-931", "done")
        self.ticket.commit()
        code, out, _ = run_main([
            "--db", str(self.ticket_path),
            "--sessions-db", str(self.sessions_path),
            "--json",
        ])
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(set(payload["summary"]), set(wait_audit.KINDS))
        # 原来那四类仍然在、各自仍然计数（`done` 这单只该命中 DONE_NO_NOTICE）：
        # 新增的 S3a 不许把自已塞进原来四类里冒充其中一员。
        original_four = (
            wait_audit.WAITING_REVIEW,
            wait_audit.DONE_NO_NOTICE,
            wait_audit.DONE_NOT_MERGED,
            wait_audit.RUNNING_IDLE,
        )
        for kind in original_four:
            self.assertIn(kind, payload["summary"])
        self.assertEqual(payload["summary"][wait_audit.DONE_NO_NOTICE], 1)
        self.assertEqual(payload["summary"][wait_audit.AWAITING_REVIEW_NO_NOTICE], 0)
        # 两个旧阈值原样在输出里，一字不变。
        self.assertEqual(payload["awaiting_review_hours"], 4.0)
        self.assertEqual(payload["idle_minutes"], 10.0)

    def test_help_lists_minutes_and_sessions_db(self):
        code, out, _ = run_main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("--minutes", out)
        self.assertIn("--sessions-db", out)


class ThresholdTest(unittest.TestCase):
    """阈值只在模块常量里写一次，且带出处：改数只能改这一处。"""

    def test_thresholds_come_from_prd_010(self):
        self.assertEqual(wait_audit.AWAITING_REVIEW_HOURS, 4.0)
        self.assertEqual(wait_audit.SESSION_IDLE_MINUTES, 10.0)
        self.assertEqual(wait_audit.AWAITING_REVIEW_NO_NOTICE_MINUTES, 20.0)

    def test_module_source_cites_prd_for_each_threshold(self):
        source = Path(wait_audit.__file__).read_text(encoding="utf-8")
        self.assertIn("PRD-010", source)
        for constant in ("AWAITING_REVIEW_HOURS", "SESSION_IDLE_MINUTES",
                         "AWAITING_REVIEW_NO_NOTICE_MINUTES"):
            index = source.index(constant)
            window = source[max(0, index - 260):index]
            self.assertIn("PRD-010", window, f"{constant} 旁边要写出处")
        # 没有出处的阈值不允许出现：这两个数是 PRD-010 里没有的口径。
        self.assertNotIn("5.0", source)
        self.assertNotIn("15.0", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
