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
import subprocess
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
from next_action import BLANK_CRITERIA_GRACE_SECONDS

from task_center.db import SCHEMA_VERSION

PROJECT = "ai-workspace"
CRITERIA_ONE = [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}]
SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "89abcdef0123456789abcdef0123456789abcdef"


def stamp(day: int) -> str:
    """固定的时间戳：2026-11-<day>T00:00:00+00:00。"""
    return f"2026-11-{day:02d}T00:00:00+00:00"


def hours_ago(hours: float) -> str:
    """相对此刻的时间戳。

    只有按「多久以前」判的那两档需要它（验收在飞的 30 分钟窗口、空判据的一天期限）：
    写死一个绝对日期的话，一条用例今天绿、明天红——年龄是相对现在算的。
    """
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=hours)).isoformat()


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
                   criteria=CRITERIA_ONE, created_at=None):
        """默认给一条可判定条件：从 AW-109 起空条件的单不派（它是念头，不是可领的活）。

        `created_at` 可以显式给：空判据那一档按建单时间算年龄，用例要靠它把一张单摆到
        期限之内或之外，不能只靠 `created_day` 的那个固定日期。
        """
        self.conn.execute(
            "INSERT INTO tickets (id, project_id, title, deliverable, prd_path,"
            " prd_sha256, criteria, revision, criteria_version, state,"
            " current_attempt, created_at, batch_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticket_id, project_id, f"{ticket_id} 的标题", "交付物", prd_path,
             "a" * 64, json.dumps(criteria, ensure_ascii=False), 1, 1, state, None,
             created_at or stamp(created_day), None),
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
        """空条件的 planned 是池子里的念头：不派出去，进 skipped 并说明原因（AW-109）。

        建单时间摆在期限之内（一小时前）：它这一轮还不该升成 `fix_pool`，只该是
        `skipped` 里的一行。躺过期限的情形在 `FixPoolTest` 里判。
        """
        self.add_ticket("AW-914", state="planned", criteria=[],
                        created_at=hours_ago(1))
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_IDLE)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.NOTHING)
        skipped = [item for item in result["skipped"] if item["reason"] == "no_criteria"]
        self.assertTrue(skipped, result)
        self.assertEqual(skipped[0]["tickets"][0]["ticket_id"], "AW-914")
        self.assertEqual(result["fix_pool"], [])

    def test_other_project_tickets_are_not_candidates(self):
        self.add_ticket("OTHER-1", state="planned", project_id="another-project")
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_IDLE)
        self.assertEqual(json.loads(out)["action"], next_action.NOTHING)


class FixPoolTest(Fixture):
    """AW-138：池子里只剩躺过期限的空判据单时，给 `fix_pool` 点名，不再报「没活」。

    这组用例要守住三件事，而且三件都只能靠两个可观测事实判（`action` 与退出码）：

    1. **看得见：** 空判据 + 超过 `BLANK_CRITERIA_GRACE_SECONDS` → `fix_pool` 并点名，
       不是 `nothing`（C1）。
    2. **分得开：** 同一张单补上判据之后回到正常动作；期限之内也不点（C2）——
       不补与补上输出一样，这条就白做了。
    3. **只报不改（C5）：** 跑完一张空判据单的现场，库里的行一个字节也不变。
    """

    def stale(self, ticket_id="AW-990", hours=None, **kwargs):
        """建一张空判据、建单时间超过期限的单。默认超过一天零一小时。"""
        hours = BLANK_CRITERIA_GRACE_SECONDS / 3600 + 1 if hours is None else hours
        self.add_ticket(ticket_id, state="planned", criteria=[],
                        created_at=hours_ago(hours), **kwargs)

    def test_stale_blank_criteria_becomes_fix_pool(self):
        self.stale("AW-990")
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_WORK)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.FIX_POOL)
        self.assertEqual(result["reason_code"], "blank_criteria")
        self.assertEqual(result["dispatch"]["ticket_id"], "AW-990")
        self.assertEqual(result["dispatch"]["ticket_ids"], ["AW-990"])
        self.assertEqual(result["candidates"][next_action.FIX_POOL], ["AW-990"])
        # 两条出路都要写在回执里：补判据 / 取消，不是「请人工确认」。
        self.assertEqual(result["dispatch"]["outcomes"],
                         ["revise_ticket", "cancel_ticket"])
        for word in ("fix_pool", "AW-990", "revise_ticket", "cancel_ticket", "只报不改"):
            self.assertIn(word, result["human"])
        # 它仍然是 skipped 里的 no_criteria：升成一档不等于从原来那张表里消失。
        self.assertTrue([item for item in result["skipped"]
                         if item["reason"] == "no_criteria"])

    def test_human_line_names_the_ticket(self):
        """人读那一行（不带 --json）也要点名：巡检靠它一眼看见。"""
        self.stale("AW-991")
        code, out, _ = self.run_main()
        self.assertEqual(code, next_action.EXIT_WORK)
        self.assertTrue(out.strip().startswith(next_action.FIX_POOL))
        self.assertIn("AW-991", out)

    def test_filling_the_criteria_brings_back_normal_action(self):
        """C2 的正向：补上判据之后就不再是 fix_pool，而是一张可领的单。"""
        self.stale("AW-992")
        code, out, _ = self.run_main("--json")
        self.assertEqual(json.loads(out)["action"], next_action.FIX_POOL)

        self.conn.execute(
            "UPDATE tickets SET criteria=?, criteria_version=2 WHERE id='AW-992'",
            (json.dumps(CRITERIA_ONE, ensure_ascii=False),),
        )
        code, out, _ = self.run_main("--json")
        result = json.loads(out)
        self.assertEqual(code, next_action.EXIT_WORK)
        self.assertEqual(result["action"], next_action.DISPATCH_EXECUTE)
        self.assertEqual(result["fix_pool"], [])

    def test_fresh_blank_criteria_is_not_named(self):
        """期限之内（一小时前建的）不点：建单与补判据本来就可能是两次动作。"""
        self.add_ticket("AW-993", state="planned", criteria=[],
                        created_at=hours_ago(1))
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_IDLE)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.NOTHING)
        self.assertEqual(result["fix_pool"], [])

    def test_threshold_boundary_comes_from_the_constant(self):
        """既不过期就不过期：差一分钟不点、过一分钟就点（阈值只认那一个常量）。"""
        grace_hours = BLANK_CRITERIA_GRACE_SECONDS / 3600
        self.add_ticket("AW-994", state="planned", criteria=[],
                        created_at=hours_ago(grace_hours - 1 / 60))
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_IDLE)
        self.assertEqual(json.loads(out)["action"], next_action.NOTHING)

        self.conn.execute(
            "UPDATE tickets SET created_at=? WHERE id='AW-994'",
            (hours_ago(grace_hours + 1 / 60),),
        )
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_WORK)
        self.assertEqual(json.loads(out)["action"], next_action.FIX_POOL)

    def test_real_work_still_wins_but_the_blank_one_is_named(self):
        """池子里另有能派的单时先派它（fix_pool 是兜底），但人话里要提到空判据那张。"""
        self.stale("AW-995")
        self.add_ticket("AW-996", state="planned", created_at=hours_ago(2))
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_WORK)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.DISPATCH_EXECUTE)
        self.assertEqual(result["dispatch"]["ticket_id"], "AW-996")
        self.assertEqual(result["candidates"][next_action.FIX_POOL], ["AW-995"])
        self.assertIn("AW-995", result["human"])
        self.assertIn("fix_pool", result["human"])

    def test_unreadable_created_at_counts_as_stale(self):
        """时间读不出来时按超期算：宁可多报一轮，也不要把它静默筛掉。"""
        self.add_ticket("AW-997", state="planned", criteria=[],
                        created_at="not-a-timestamp")
        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_WORK)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.FIX_POOL)
        self.assertTrue(result["fix_pool"][0]["timestamp_unreadable"])
        self.assertIn("读不出来", result["human"])

    def test_next_action_never_writes_to_the_pool(self):
        """C5：只报不改——跑完一轮，库里的行一个字节也不变（不写判据、不取消、不改状态）。"""
        self.stale("AW-998")
        self.add_ticket("AW-999", state="planned", created_at=hours_ago(2))
        self.conn.commit()

        def snapshot():
            return (
                [tuple(row) for row in self.conn.execute(
                    "SELECT id, state, criteria, criteria_version, revision,"
                    " current_attempt, created_at FROM tickets ORDER BY id")],
                self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],
                self.conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0],
            )

        before = snapshot()
        self.run_main("--json")
        self.run_main()
        self.assertEqual(snapshot(), before)
        # 库连接是只读打开的：写不进东西，不是靠「记得不写」。
        with self.assertRaises(sqlite3.OperationalError):
            self.conn_is_read_only()

    def conn_is_read_only(self):
        """`next_action.read_only()` 打开的那条连接必须拒绝写入（C5 的机制层）。"""
        conn = next_action.read_only(self.db_path)
        try:
            conn.execute("UPDATE tickets SET state='cancelled' WHERE id='AW-998'")
        finally:
            conn.close()


class FixPoolThrottleTest(Fixture):
    """AW-140：同一批空判据单不许每轮一字不变地重报——报过要退避，报到第 N 轮要升级。

    这一组只看两个可观测事实（`action` / `reason_code` 与退出码），但每个用例都要造一份
    **落库的台账**：`record_pool_report` 事件。台账不在会话上下文里，所以每个用例开头都
    从空库起算，中途只通过 `report()` 往库里加行——这正是「换一个会话也读得到」的形状。
    """

    def stale(self, ticket_id="AW-970", state="planned"):
        """一张空判据、建单时间已经超过宽限期的单。"""
        hours = BLANK_CRITERIA_GRACE_SECONDS / 3600 + 1
        self.add_ticket(ticket_id, state=state, criteria=[], created_at=hours_ago(hours))

    def report(self, ticket_id, hours_ago_value=0.5, request_id=None):
        """落一条「已经点名报过」的事件：一条行 = 一次点名（与 task_center 的写法同形）。"""
        detail = {"request_id": request_id or f"req-{ticket_id}-{hours_ago_value}",
                  "result": {"ticket_id": ticket_id, "reported_rounds": 0}}
        self.add_events_table_row(ticket_id, next_action.POOL_REPORT,
                                  detail, hours_ago(hours_ago_value))

    def scan_json(self):
        code, out, _ = self.run_main("--json")
        return code, json.loads(out)

    def test_third_report_escalates_instead_of_repeating(self):
        """报到第 N 轮：这一条不再是「补判据还是取消」的原话，而是「去升级」。"""
        self.stale("AW-970")
        self.report("AW-970", hours_ago_value=8)
        self.report("AW-970", hours_ago_value=7)
        code, result = self.scan_json()
        self.assertEqual(code, next_action.EXIT_WORK)
        self.assertEqual(result["action"], next_action.FIX_POOL)
        self.assertEqual(result["reason_code"], "blank_criteria_escalate")
        self.assertTrue(result["dispatch"]["escalate"])
        self.assertEqual(result["dispatch"]["reported_rounds"], {"AW-970": 2})
        self.assertEqual(result["dispatch"]["escalation"]["ticket_ids"], ["AW-970"])
        self.assertEqual(result["dispatch"]["escalation"]["options"],
                         ["create_ticket:planned", "ask_ticket"])
        for word in ("升级", "planned", "ask_ticket", "revise_ticket", "cancel_ticket"):
            self.assertIn(word, result["human"])

    def test_inside_the_backoff_window_it_is_not_repeated(self):
        """报过一次、还没到点：这一轮不点名，退出 2（调度侧不开会话）——但台账照旧读得出。"""
        self.stale("AW-971")
        self.report("AW-971", hours_ago_value=0.5)
        code, result = self.scan_json()
        self.assertEqual(code, next_action.EXIT_IDLE)
        self.assertEqual(result["action"], next_action.NOTHING)
        self.assertEqual(result["reason_code"], "blank_criteria_backoff")
        entry = result["fix_pool"][0]
        self.assertEqual(entry["reported_rounds"], 1)
        self.assertFalse(entry["due"])
        self.assertGreater(entry["seconds_until_next_report"], 0)
        # 名字、轮次与还剩多久都还在输出里：不点名不等于看不见。
        self.assertIn("AW-971", result["human"])
        self.assertIn("退避", result["human"])

    def test_round_two_is_due_after_the_first_window(self):
        """第一次之后等 2 小时：过了窗口就照原样点名，并带上「已报过 1 轮」。"""
        self.stale("AW-972")
        self.report("AW-972", hours_ago_value=3)
        code, result = self.scan_json()
        self.assertEqual(code, next_action.EXIT_WORK)
        self.assertEqual(result["action"], next_action.FIX_POOL)
        self.assertEqual(result["reason_code"], "blank_criteria")
        self.assertFalse(result["dispatch"]["escalate"])
        self.assertEqual(result["dispatch"]["reported_rounds"], {"AW-972": 1})

    def test_the_streak_is_read_from_the_database_not_the_session(self):
        """C2 的反例在这里失效：报过之后，再跑一次（等同换会话）不会从第 1 轮重算。"""
        self.stale("AW-973")
        first_code, first = self.scan_json()
        self.assertEqual(first_code, next_action.EXIT_WORK)
        self.assertEqual(first["reason_code"], "blank_criteria")
        self.assertEqual(first["fix_pool"][0]["reported_rounds"], 0)

        # 上一次现场（任何会话）落下的记录就是全部状态：模型里没有别的东西可丢。
        self.report("AW-973", hours_ago_value=0.2, request_id="req-AW-973-1")
        second_code, second = self.scan_json()
        self.assertEqual(second_code, next_action.EXIT_IDLE)
        self.assertEqual(second["reason_code"], "blank_criteria_backoff")
        self.assertEqual(second["fix_pool"][0]["reported_rounds"], 1)

    def test_escalation_is_announced_once_then_backed_off(self):
        """升级过一次之后不许每轮都喊「请升级」：此后按 24 小时一次。"""
        self.stale("AW-974")
        for hours in (40, 32, 0.5):
            self.report("AW-974", hours_ago_value=hours)
        code, result = self.scan_json()
        self.assertEqual(code, next_action.EXIT_IDLE)
        self.assertEqual(result["reason_code"], "blank_criteria_backoff")
        self.assertEqual(result["fix_pool"][0]["reported_rounds"], 3)
        self.assertEqual(result["fix_pool"][0]["backoff_seconds"],
                         next_action.POOL_REPORT_BACKOFF_SECONDS[-1])

        # 24 小时之后才轮到下一次：仍然是升级那一句，不是每轮重喊。
        self.conn.execute(
            "UPDATE events SET created_at=? WHERE ticket_id='AW-974' AND operation=?",
            (hours_ago(25), next_action.POOL_REPORT))
        code, result = self.scan_json()
        self.assertEqual(code, next_action.EXIT_WORK)
        self.assertEqual(result["reason_code"], "blank_criteria_escalate")

    def test_real_work_still_wins_while_the_blank_one_is_due(self):
        """C3：有可派的单时先给那个——节流不许把真活压住，也不许把这一档提到前面。"""
        self.stale("AW-975")
        self.report("AW-975", hours_ago_value=8)
        self.report("AW-975", hours_ago_value=7)
        self.add_ticket("AW-976", state="planned", created_at=hours_ago(2))
        code, result = self.scan_json()
        self.assertEqual(code, next_action.EXIT_WORK)
        self.assertEqual(result["action"], next_action.DISPATCH_EXECUTE)
        self.assertEqual(result["dispatch"]["ticket_id"], "AW-976")
        self.assertEqual(result["candidates"][next_action.FIX_POOL], ["AW-975"])
        self.assertIn("AW-975", result["human"])

    def test_mixed_due_and_backoff_names_both(self):
        """一张到点、一张还在窗口里：点名的只有到点那张，退避那张也要写进人话。"""
        self.stale("AW-980")
        self.report("AW-980", hours_ago_value=8)
        self.report("AW-980", hours_ago_value=7)
        self.stale("AW-981")
        self.report("AW-981", hours_ago_value=0.1)
        code, result = self.scan_json()
        self.assertEqual(code, next_action.EXIT_WORK)
        self.assertEqual(result["dispatch"]["ticket_ids"], ["AW-980"])
        self.assertEqual(result["candidates"][next_action.FIX_POOL], ["AW-980", "AW-981"])
        self.assertIn("另有 1 张空判据单还在退避窗口里", result["human"])

    def test_rejection_row_does_not_count_as_a_report(self):
        """被拒的调用也留一行同名事件：它带 `rejected`，不是「已经报过」。"""
        self.stale("AW-977")
        self.add_events_table_row("AW-977", next_action.POOL_REPORT,
                                  {"rejected": "INVALID_STATE", "request_id": "req-x"},
                                  hours_ago(0.2))
        code, result = self.scan_json()
        self.assertEqual(code, next_action.EXIT_WORK)
        self.assertEqual(result["reason_code"], "blank_criteria")
        self.assertEqual(result["fix_pool"][0]["reported_rounds"], 0)

    def test_unreadable_report_time_does_not_silence_the_ticket(self):
        """点名记录的时间读不出来时按该报处理：宁可多叫一次，也不押住它。"""
        self.stale("AW-978")
        self.add_events_table_row("AW-978", next_action.POOL_REPORT,
                                  {"request_id": "req-y", "result": {"ticket_id": "AW-978"}},
                                  "not-a-timestamp")
        code, result = self.scan_json()
        self.assertEqual(code, next_action.EXIT_WORK)
        self.assertEqual(result["reason_code"], "blank_criteria")
        self.assertTrue(result["fix_pool"][0]["report_timestamp_unreadable"])

    def test_thresholds_are_echoed_from_the_constants(self):
        """阈值只有一个来源：输出里的数就是那几个常量，再没有第二个 2/6/24。"""
        self.stale("AW-979")
        _, result = self.scan_json()
        self.assertEqual(result["pool_report"]["escalate_after"],
                         next_action.POOL_REPORT_ESCALATE_AFTER)
        self.assertEqual(result["pool_report"]["backoff_seconds"],
                         list(next_action.POOL_REPORT_BACKOFF_SECONDS))
        self.assertEqual(result["pool_report"]["operation"], next_action.POOL_REPORT)


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


class MergeCriteriaTest(Fixture):
    """AWK-114：合并候选与 `auto_merge --list` 用同一套口径。

    工单说的矛盾现场是：单已经 `done`、`merges` 里没有行，`auto_merge --list` 把它
    列进「真待合并」，而 `next_action` 照旧报 `dispatch_execute`。根因不是顺序，是
    判据少了一层——`next_action` 只认 `submissions.head_sha` 这一个事实，既没读
    历史豁免清单，也没看 `ticket/<单号>` 分支在不在。

    所以这里的用例按「事实」造，不按实现造：有分支头的、有分支没分支头的、
    在豁免清单里的、真历史直做的，各一条。最后一条把两个工具的输出直接对一遍。
    """

    def setUp(self):
        super().setUp()
        self.exemptions = self.root / "docs" / "unmerged-exemptions.json"

    # ---- 临时仓库与豁免清单

    def git(self, *args):
        proc = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def init_repo(self):
        """把项目根做成一个真仓库：分支在不在这件事只能问 git，造不出来就判不了。"""
        self.git("init", "-q", "-b", "master")
        self.git("-c", "user.email=t@example.com", "-c", "user.name=t",
                 "commit", "-q", "--allow-empty", "-m", "base")

    def merged_ticket_branch(self, ticket_id):
        """建一条已经合进 master 的 `ticket/<单号>` 分支，返回它的 tip。

        这就是 C2 的「分支已合入」现场：合并发生了，`merges` 里没有行。
        """
        branch = f"ticket/{ticket_id}"
        self.git("checkout", "-q", "-b", branch)
        self.git("-c", "user.email=t@example.com", "-c", "user.name=t",
                 "commit", "-q", "--allow-empty", "-m", f"{ticket_id} 的成果")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "master")
        self.git("-c", "user.email=t@example.com", "-c", "user.name=t",
                 "merge", "-q", "--no-ff", branch, "-m", f"Merge {branch}")
        return tip

    def write_exemptions(self, ticket_ids):
        """照 `tools/backfill_merges.py --apply` 写出来的形状写一份清单。"""
        self.exemptions.parent.mkdir(parents=True, exist_ok=True)
        self.exemptions.write_text(json.dumps({
            "generated_at": "2026-11-23T00:00:00+00:00",
            "target_branch": "master",
            "entries": [{"ticket_id": ticket_id, "project_id": PROJECT,
                         "reason": "no_head_sha", "reason_text": "最新提交没有 head_sha"}
                        for ticket_id in ticket_ids],
        }, ensure_ascii=False), encoding="utf-8")

    def done_ticket(self, ticket_id, head_sha=SHA, created_day=19):
        self.add_ticket(ticket_id, state="done", created_day=created_day)
        self.add_attempt(ticket_id, f"a-{ticket_id}", state="accepted")
        self.add_submission(f"a-{ticket_id}", f"sub-{ticket_id}",
                            head_sha=head_sha, created_day=created_day)
        self.add_notice(ticket_id)

    # ---- C2 / C3：命中就报 merge，补上记录就不再报

    def test_done_without_merge_record_is_merge_not_dispatch_execute(self):
        """C2：分支已合入、`merges` 里没有行，池子里另有可领单时也不许派新单。"""
        self.init_repo()
        tip = self.merged_ticket_branch("AW-961")
        self.done_ticket("AW-961", head_sha=tip)
        self.add_ticket("AW-962", state="planned", created_day=20)

        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_WORK)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.MERGE)
        self.assertEqual(result["reason_code"], "done_not_merged")
        self.assertEqual(result["dispatch"]["ticket_id"], "AW-961")
        self.assertEqual(result["candidates"][next_action.MERGE], ["AW-961"])
        self.assertIn("AW-961", result["human"])
        self.assertEqual(result["candidates"][next_action.DISPATCH_EXECUTE], ["AW-962"])

    def test_merge_record_clears_the_candidate(self):
        """C3：补上 `merges` 行之后不再报 merge，池子里的单这才该派出去。"""
        self.init_repo()
        tip = self.merged_ticket_branch("AW-963")
        self.done_ticket("AW-963", head_sha=tip)
        self.add_ticket("AW-964", state="planned", created_day=20)

        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_WORK)
        self.assertEqual(json.loads(out)["action"], next_action.MERGE)

        self.add_merge("AW-963")
        code, out, _ = self.run_main("--json")
        result = json.loads(out)
        self.assertEqual(code, next_action.EXIT_WORK)
        self.assertEqual(result["action"], next_action.DISPATCH_EXECUTE)
        self.assertEqual(result["candidates"][next_action.MERGE], [])
        self.assertNotIn("AW-963", result["human"])

    # ---- 与 auto_merge --list 对齐的两条边界

    def test_ticket_branch_without_reviewed_sha_is_still_a_merge_candidate(self):
        """提交里没有分支头、但 `ticket/<单号>` 分支还在：`auto_merge --list` 算真待合并。"""
        self.init_repo()
        self.done_ticket("AW-965", head_sha=None)
        self.git("branch", "ticket/AW-965")

        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_WORK)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.MERGE)
        self.assertEqual(result["dispatch"]["ticket_id"], "AW-965")
        self.assertIsNone(result["dispatch"]["head_sha"])
        self.assertIn("unverified", " ".join(result["stop"]))

    def test_historical_direct_ticket_is_not_a_merge_candidate(self):
        """C4：done、无合并记录、提交没分支头、也没有 `ticket/<单号>` 分支 = 历史直做。"""
        self.init_repo()
        self.done_ticket("AW-966", head_sha=None)

        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_IDLE)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.NOTHING)
        self.assertEqual(result["candidates"][next_action.MERGE], [])
        self.assertEqual(result["skipped"][0]["reason"], next_action.SKIP_NO_BRANCH_HEAD)
        self.assertEqual(result["skipped"][0]["ticket_ids"], ["AW-966"])

    def test_exempted_ticket_is_not_a_merge_candidate(self):
        """C4：已在 `docs/unmerged-exemptions.json` 里 → 不算待合并（与分栏口径一致）。"""
        self.init_repo()
        self.done_ticket("AW-967", head_sha=None)
        self.write_exemptions(["AW-967"])

        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_IDLE)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.NOTHING)
        self.assertEqual(result["candidates"][next_action.MERGE], [])
        self.assertEqual(result["skipped"][0]["reason"], next_action.SKIP_EXEMPTED)
        self.assertEqual(result["skipped"][0]["ticket_ids"], ["AW-967"])

    def test_exempted_ticket_with_a_contributing_branch_is_still_exempt(self):
        """清单先于分支：豁免单就算分支还躺着，`auto_merge --list` 也不把它算真待合并。"""
        self.init_repo()
        self.done_ticket("AW-968", head_sha=None)
        self.git("branch", "ticket/AW-968")
        self.write_exemptions(["AW-968"])

        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_IDLE)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.NOTHING)
        self.assertEqual(result["skipped"][0]["reason"], next_action.SKIP_EXEMPTED)

    # ---- 交叉用例：两个工具对同一现场给同一份清单

    def test_list_and_next_action_agree_on_real_pending(self):
        """`auto_merge --list` 的「真待合并」与 `next_action` 的 `merge` 候选逐字相等。"""
        import auto_merge

        self.init_repo()
        tip = self.merged_ticket_branch("AW-969")
        self.done_ticket("AW-969", head_sha=tip)
        self.done_ticket("AW-970", head_sha=None, created_day=18)
        self.git("branch", "ticket/AW-970")
        self.done_ticket("AW-971", head_sha=None, created_day=17)
        self.write_exemptions(["AW-971"])
        self.conn.commit()

        real_column, exempt_column = _list_columns(
            auto_merge, _FakeCenter(self.conn), self.exemptions)
        result = next_action.scan(
            self.conn, PROJECT, pause_path=self.pause, cwd=self.root
        )
        self.assertEqual(sorted(real_column), ["AW-969", "AW-970"])
        # 连顺序也要一样：两张待合并的单按 `created_at` 排，两件工具各排一次就会分歧。
        self.assertEqual(result["candidates"][next_action.MERGE], real_column)
        self.assertEqual(exempt_column, ["AW-971"])
        self.assertEqual(result["skipped"][0]["reason"], next_action.SKIP_EXEMPTED)
        self.assertEqual(result["skipped"][0]["ticket_ids"], exempt_column)

    def test_unexempted_ticket_without_branch_names_the_stale_snapshot(self):
        """清单快照旧了的那一档：`auto_merge` 仍把它列进真待合并，`next_action`
        不派动作，但把「快照该刷新了」写在 `skipped` 里——两边都不隐去它。"""
        import auto_merge

        self.init_repo()
        self.done_ticket("AW-972", head_sha=None)
        self.write_exemptions([])
        self.conn.commit()

        real_column, _ = _list_columns(
            auto_merge, _FakeCenter(self.conn), self.exemptions)
        self.assertEqual(real_column, ["AW-972"])

        code, out, _ = self.run_main("--json")
        self.assertEqual(code, next_action.EXIT_IDLE)
        result = json.loads(out)
        self.assertEqual(result["action"], next_action.NOTHING)
        self.assertEqual(result["candidates"][next_action.MERGE], [])
        skipped = result["skipped"][0]
        self.assertEqual(skipped["reason"], next_action.SKIP_NO_BRANCH_HEAD)
        self.assertEqual(skipped["ticket_ids"], ["AW-972"])
        self.assertIn("backfill_merges", skipped["detail"])


def _list_columns(auto_merge, center, exemptions_path):
    """从 `auto_merge --list` 的原文里抽出「真待合并」与「历史豁免」两栏。

    不调内部函数取集合，而是读它真打印的那几行：这张清单是给人看的，口径变了
    首先要变的就是这几行，用例跟着它走才拦得住「实现改了输出没改」。
    """
    lines = []
    auto_merge.list_unmerged_rows(center, lines.append, str(exemptions_path))
    real, exempt = [], []
    section = None
    for line in lines:
        if line.startswith("真待合并"):
            section = "real"
            continue
        if line.startswith("历史豁免"):
            section ="exempt"
            continue
        if not line.strip():
            continue
        if line.startswith("  （没有）"):
            continue
        if section == "real" and line.startswith("  ") and "\t" in line:
            real.append(line.strip().split("\t")[0])
        elif section == "exempt" and line.startswith("  ") and "\t" in line:
            exempt.append(line.strip().split("\t")[0])
    return real, exempt


class _FakeCenter:
    """只给 `auto_merge.list_unmerged_rows` 用：按 `task_center` 的回执形状喂库里的行。

    这是这条交叉用例唯一的替身：它把 `list_unmerged` / `get_ticket` 两个只读查询
    翻译成这份临时库上的 SQL。两个工具的分栏口径因此可以对着同一份库掰一次。
    """

    def __init__(self, conn):
        self.conn = conn

    def execute(self, operation, payload=None):
        if operation == "list_unmerged":
            rows = self.conn.execute(
                "SELECT t.id, t.project_id, t.title, t.state, t.revision, t.created_at"
                " FROM tickets t LEFT JOIN merges m ON m.ticket_id=t.id"
                " WHERE t.state='done' AND m.id IS NULL"
                " ORDER BY t.created_at, t.id"
            ).fetchall()
            items = [{"id": row[0], "project_id": row[1], "title": row[2],
                      "state": row[3], "revision": row[4], "created_at": row[5]}
                     for row in rows]
            return {"ok": True, "result": {"items": items, "count": len(items)}}
        if operation == "get_ticket":
            rows = self.conn.execute(
                "SELECT s.head_sha FROM submissions s JOIN attempts a ON a.id=s.attempt_id"
                " WHERE a.ticket_id=? ORDER BY s.rowid", (payload["ticket_id"],)
            ).fetchall()
            return {"ok": True,
                    "result": {"submissions": [{"head_sha": row[0]} for row in rows]}}
        raise AssertionError(f"这条用例没有为 {operation} 准备替身")



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
