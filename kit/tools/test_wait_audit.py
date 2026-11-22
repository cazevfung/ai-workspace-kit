#!/usr/bin/env python
"""`tools/wait_audit.py` 的用例：四类各造一条命中 + 一条不命中，再验退出码。

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
    """S3：4 小时且没有 review 行才报；有 review 行、或没到 4 小时都不报。"""

    def test_hit_after_four_hours_without_review(self):
        add_ticket(self.ticket, "AW-901", "awaiting_review")
        add_attempt(self.ticket, "AW-901", "a-901", state="submitted")
        add_submission(self.ticket, "a-901", "s-901", minutes_ago=5 * 60)
        result = self.run_scan()
        self.assertIn(wait_audit.WAITING_REVIEW, self.kinds(result))
        self.assertEqual(result["summary"][wait_audit.WAITING_REVIEW], 1)
        self.assertEqual(result["hits"][-1]["ticket_id"], "AW-901")

    def test_not_hit_before_four_hours(self):
        add_ticket(self.ticket, "AW-902", "awaiting_review")
        add_attempt(self.ticket, "AW-902", "a-902", state="submitted")
        add_submission(self.ticket, "a-902", "s-902", minutes_ago=3 * 60 + 59)
        result = self.run_scan()
        self.assertNotIn(wait_audit.WAITING_REVIEW, self.kinds(result))

    def test_not_hit_when_review_row_exists(self):
        add_ticket(self.ticket, "AW-903", "awaiting_review")
        add_attempt(self.ticket, "AW-903", "a-903", state="submitted")
        add_submission(self.ticket, "a-903", "s-903", minutes_ago=9 * 60)
        add_review(self.ticket, "s-903")
        result = self.run_scan()
        self.assertEqual(result["hits"], [])
        reasons = [item["reason"] for item in result["ignored"]]
        self.assertIn("already_reviewed", reasons)


def add_notice(conn, ticket_id, status, notice_id=None):
    conn.execute(
        "INSERT INTO notices (id, ticket_id, status) VALUES (?,?,?)",
        (notice_id or f"n-{ticket_id}-{status}", ticket_id, status),
    )


class AwaitingReceiptTest(TempDbCase):
    """回执那一环：awaiting_review 且超过 15 分钟没有任何一条 sent 回执就报。

    起因是 2026-11-22 的实测：AWK-101 的执行会话提交完就走了，一条回执也没发，
    `wait_audit` 一声没响（它只报「等在验收超过 4 小时」），靠人追问才发现。
    """

    def test_hit_when_only_failed_notice_and_past_fifteen_minutes(self):
        add_ticket(self.ticket, "AW-940", "awaiting_review")
        add_attempt(self.ticket, "AW-940", "a-940", state="submitted")
        add_submission(self.ticket, "a-940", "s-940", minutes_ago=20)
        add_notice(self.ticket, "AW-940", "failed")
        result = self.run_scan()
        self.assertEqual(self.kinds(result), [wait_audit.AWAITING_RECEIPT])
        self.assertEqual(result["summary"][wait_audit.AWAITING_RECEIPT], 1)
        self.assertEqual(result["hits"][0]["ticket_id"], "AW-940")

    def test_hit_when_no_notice_rows_at_all(self):
        add_ticket(self.ticket, "AW-941", "awaiting_review")
        add_attempt(self.ticket, "AW-941", "a-941", state="submitted")
        add_submission(self.ticket, "a-941", "s-941", minutes_ago=30)
        result = self.run_scan()
        self.assertEqual(self.kinds(result), [wait_audit.AWAITING_RECEIPT])

    def test_not_hit_before_fifteen_minutes(self):
        add_ticket(self.ticket, "AW-942", "awaiting_review")
        add_attempt(self.ticket, "AW-942", "a-942", state="submitted")
        add_submission(self.ticket, "a-942", "s-942", minutes_ago=10)
        result = self.run_scan()
        self.assertEqual(result["hits"], [])

    def test_boundary_is_strictly_past_fifteen_minutes(self):
        add_ticket(self.ticket, "AW-943", "awaiting_review")
        add_attempt(self.ticket, "AW-943", "a-943", state="submitted")
        add_submission(self.ticket, "a-943", "s-943",
                       minutes_ago=wait_audit.AWAITING_RECEIPT_MINUTES)
        add_ticket(self.ticket, "AW-949", "awaiting_review")
        add_attempt(self.ticket, "AW-949", "a-949", state="submitted")
        add_submission(self.ticket, "a-949", "s-949",
                       minutes_ago=wait_audit.AWAITING_RECEIPT_MINUTES + 0.5)
        result = self.run_scan()
        self.assertEqual([hit["ticket_id"] for hit in result["hits"]], ["AW-949"])

    def test_failed_notice_is_not_a_delivered_receipt(self):
        """`failed` 只说明曾经想发没发成：它在库里，但一样要报（AWK-101 的现场）。

        判据只看有没有 `sent`。把 `failed` 当成「走过回执」等于把「想发过」当成「发到了」，
        那一单就会停在 `awaiting_review` 且没有任何东西去催。
        """
        add_ticket(self.ticket, "AW-939", "awaiting_review")
        add_attempt(self.ticket, "AW-939", "a-939", state="submitted")
        add_submission(self.ticket, "a-939", "s-939", minutes_ago=20)
        add_notice(self.ticket, "AW-939", "failed", notice_id="n-939-a")
        add_notice(self.ticket, "AW-939", "failed", notice_id="n-939-b")
        result = self.run_scan()
        self.assertEqual(self.kinds(result), [wait_audit.AWAITING_RECEIPT])
        self.assertIn("failed×2", result["hits"][0]["detail"])
        self.assertNotIn("notice_sent", [item["reason"] for item in result["ignored"]])

    def test_not_hit_when_sent_notice_exists(self):
        add_ticket(self.ticket, "AW-944", "awaiting_review")
        add_attempt(self.ticket, "AW-944", "a-944", state="submitted")
        add_submission(self.ticket, "a-944", "s-944", minutes_ago=30)
        add_notice(self.ticket, "AW-944", "sent")
        result = self.run_scan()
        self.assertEqual(result["hits"], [])
        reasons = [item["reason"] for item in result["ignored"]]
        self.assertIn("notice_sent", reasons)

    def test_not_hit_when_review_row_exists(self):
        add_ticket(self.ticket, "AW-945", "awaiting_review")
        add_attempt(self.ticket, "AW-945", "a-945", state="submitted")
        add_submission(self.ticket, "a-945", "s-945", minutes_ago=30)
        add_review(self.ticket, "s-945")
        result = self.run_scan()
        self.assertEqual(result["hits"], [])
        reasons = [item["reason"] for item in result["ignored"]]
        self.assertIn("already_reviewed", reasons)

    def test_not_hit_when_ticket_is_not_awaiting_review(self):
        add_ticket(self.ticket, "AW-946", "running")
        add_attempt(self.ticket, "AW-946", "a-946", state="running")
        add_submission(self.ticket, "a-946", "s-946", minutes_ago=30)
        result = self.run_scan()
        self.assertNotIn(wait_audit.AWAITING_RECEIPT, self.kinds(result))

    def test_human_line_shows_waiting_minutes(self):
        add_ticket(self.ticket, "AW-947", "awaiting_review")
        add_attempt(self.ticket, "AW-947", "a-947", state="submitted")
        add_submission(self.ticket, "a-947", "s-947", minutes_ago=20)
        rendered = wait_audit.render_human(self.run_scan())
        self.assertIn("awaiting_receipt 20.0min AW-947", rendered)

    def test_counted_separately_from_waiting_review(self):
        """两类各数各的：回执没发与没人验收是两条链，不合并成一条。"""
        add_ticket(self.ticket, "AW-948", "awaiting_review")
        add_attempt(self.ticket, "AW-948", "a-948", state="submitted")
        add_submission(self.ticket, "a-948", "s-948", minutes_ago=5 * 60)
        result = self.run_scan()
        self.assertEqual(sorted(self.kinds(result)),
                         sorted([wait_audit.AWAITING_RECEIPT, wait_audit.WAITING_REVIEW]))


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
    """退出码与输出形状：末行固定四类合计，缺库必须是 2 而不是 0。"""

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

    def test_json_output_has_all_counts(self):
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
        self.assertEqual(payload["summary"][wait_audit.DONE_NO_NOTICE], 1)

    def test_exit_one_with_awaiting_receipt(self):
        """提交 20 分钟、回执只失败过一次：退出码 1，且点名 awaiting_receipt。

        时间从真实 now 往前推：`main()` 自己取 now()，钉在夹具那个固定 NOW 上会漂。
        """
        real_now = datetime.datetime.now(datetime.timezone.utc)
        add_ticket(self.ticket, "AW-950", "awaiting_review")
        add_attempt(self.ticket, "AW-950", "a-950", state="submitted")
        self.ticket.execute(
            "INSERT INTO submissions (id, attempt_id, created_at) VALUES (?,?,?)",
            ("s-950", "a-950", iso(20, now=real_now)),
        )
        add_notice(self.ticket, "AW-950", "failed")
        self.ticket.commit()
        code, out, _ = run_main([
            "--db", str(self.ticket_path),
            "--sessions-db", str(self.sessions_path),
        ])
        self.assertEqual(code, 1)
        self.assertIn(wait_audit.AWAITING_RECEIPT, out)
        self.assertIn("等回执 1 条", out.strip().splitlines()[-1])

    def test_help_lists_minutes_and_sessions_db(self):
        code, out, _ = run_main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("--minutes", out)
        self.assertIn("--sessions-db", out)


class ThresholdTest(unittest.TestCase):
    """阈值只在模块常量里写一次，且每个都带出处：改数只能改这一处。"""

    def test_thresholds_come_from_prd_010(self):
        self.assertEqual(wait_audit.AWAITING_REVIEW_HOURS, 4.0)
        self.assertEqual(wait_audit.SESSION_IDLE_MINUTES, 10.0)

    def test_awaiting_receipt_threshold_is_fifteen_minutes(self):
        self.assertEqual(wait_audit.AWAITING_RECEIPT_MINUTES, 15.0)

    def test_module_source_cites_the_source_of_each_threshold(self):
        source = Path(wait_audit.__file__).read_text(encoding="utf-8")
        # 前两个数出自 PRD-010 §4.3；15 分钟不是 PRD 里的数，出处是 2026-11-22 的实测。
        for constant in ("AWAITING_REVIEW_HOURS", "SESSION_IDLE_MINUTES"):
            index = source.index(constant)
            window = source[max(0, index - 200):index]
            self.assertIn("PRD-010", window, f"{constant} 旁边要写出处")
        index = source.index("AWAITING_RECEIPT_MINUTES")
        window = source[max(0, index - 400):index]
        self.assertIn("AWK-101", window, "15 分钟旁边要写出它的实测出处")
        # 没有出处的阈值不允许出现（搜「= 5.0」而不是「5.0」：15.0 里也会命中子串）。
        self.assertNotRegex(source, r"=\s*5\.0\b")


if __name__ == "__main__":
    unittest.main(verbosity=2)
