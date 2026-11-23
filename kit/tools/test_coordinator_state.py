"""`tools/coordinator_state.py` 的用例：这张单现在的责任人还在不在，一条命令给结论。

这条命令存在的理由：AW-114 的现场是「负责收尾的会话已被宿主归档，但库里只有它的 id」。
判「要不要接替」以前要开宿主会话库手查 `sessions.status`；现在这一步变成一个能复算的
动作，而且三种结论各有退出码——`unknown` 不许长得像「正常」。
"""
import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import coordinator_state  # noqa: E402

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "89abcdef0123456789abcdef0123456789abcdef"
STAMP_MS = 1795334648266  # 2026-11-22T08:04:08.266Z，AW-106 那条协调会话的 updated_at


def build_sessions(path, rows):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT, working_dir TEXT,"
                     " status TEXT, source TEXT, created_at INTEGER, updated_at INTEGER,"
                     " last_turn_ended_at INTEGER, hidden_at TEXT)")
        for row in rows:
            conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)", row)
        conn.commit()
    finally:
        conn.close()
    return path


class CoordinatorStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "项目一"
        self.project.mkdir()
        (self.project / "PRD.md").write_text("需求第一版", encoding="utf-8")
        self.db = self.root / "center.sqlite3"
        self.sessions_db = self.root / "cindy-fake.db"
        from task_center import Center
        self.center = Center(self.db)
        self.ids = {}
        self.counter = 0
        self.ok("init")
        self.ok("register_project", {"project_id": "p1", "name": "项目一", "root": str(self.project)})

    # --- 夹具 -------------------------------------------------------------

    def call(self, op, payload=None):
        self.counter += 1
        payload = payload or {}
        alias = payload.get("ticket_id")
        if isinstance(alias, str) and alias in self.ids:
            payload = {**payload, "ticket_id": self.ids[alias]}
        return self.center.execute(op, payload, f"req-{self.counter}")

    def ok(self, op, payload=None):
        result = self.call(op, payload)
        self.assertTrue(result["ok"], result)
        return result["result"]

    def dispatched(self, coordinator="coordinator-1"):
        created = self.ok("create_ticket", {
            "project_id": "p1", "title": "整理检查清单", "deliverable": "一份清单",
            "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        })
        self.ids["t1"] = created["id"]
        claim = self.ok("claim_ticket", {"project_id": "p1", "ticket_id": "t1",
                                         "expected_revision": 1,
                                         "coordinator_session_id": coordinator})
        self.ok("bind_session", {"project_id": "p1", "ticket_id": "t1",
                                 "attempt_id": claim["attempt_id"], "session_id": "executor-1",
                                 "project_root": str(self.project)})
        return claim["attempt_id"]

    def run_cli(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = coordinator_state.main(["--ticket", self.ids["t1"], "--db", str(self.db),
                                           "--sessions-db", str(self.sessions_db), *extra])
        return code, out.getvalue()

    # --- 三种结论，各有退出码 ---------------------------------------------

    def test_archived_owner_exits_one(self):
        attempt = self.dispatched()
        build_sessions(self.sessions_db, [
            ("coordinator-1", "[Schedule] 派单器", "{{WORKSPACE}}", "archived", "scheduler",
             1795334583341, STAMP_MS, 1795334648244, None)])
        code, out = self.run_cli()
        self.assertEqual(1, code)
        self.assertIn("coordinator-1", out)
        self.assertIn("archived", out)
        self.assertIn("TAKEOVER_NEEDED", out)
        self.assertIn("take_over_ticket", out)
        self.assertIn("2026-11-22T08:04:08", out)

    def test_active_owner_exits_zero(self):
        self.dispatched()
        build_sessions(self.sessions_db, [
            ("coordinator-1", "协调会话", "{{WORKSPACE}}", "active", "desktop",
             1795334583341, STAMP_MS, 1795334648244, None)])
        code, out = self.run_cli()
        self.assertEqual(0, code)
        self.assertIn("OWNER_ACTIVE", out)
        self.assertNotIn("TAKEOVER_NEEDED", out)

    def test_a_missing_owner_row_also_asks_for_a_takeover(self):
        self.dispatched()
        build_sessions(self.sessions_db, [])
        code, out = self.run_cli()
        self.assertEqual(1, code)
        self.assertIn("missing", out)
        self.assertIn("TAKEOVER_NEEDED", out)

    def test_an_unreadable_sessions_db_is_not_reported_as_fine(self):
        self.dispatched()
        self.sessions_db.unlink(missing_ok=True)
        code, out = self.run_cli()
        self.assertEqual(2, code)
        self.assertIn("UNKNOWN", out)

    def test_a_ticket_without_an_owner_is_not_reported_as_fine(self):
        created = self.ok("create_ticket", {
            "project_id": "p1", "title": "还没派发", "deliverable": "一份清单",
            "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        })
        self.ids["t1"] = created["id"]
        build_sessions(self.sessions_db, [])
        code, out = self.run_cli()
        self.assertEqual(2, code)
        self.assertIn("NO_OWNER", out)

    def test_an_unknown_ticket_exits_two(self):
        self.dispatched()
        build_sessions(self.sessions_db, [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = coordinator_state.main(["--ticket", "AW-999", "--db", str(self.db),
                                           "--sessions-db", str(self.sessions_db)])
        self.assertEqual(2, code)

    def test_lookup_returns_the_fields_without_printing(self):
        attempt = self.dispatched()
        build_sessions(self.sessions_db, [
            ("coordinator-1", "协调会话", "{{WORKSPACE}}", "archived", "scheduler",
             1795334583341, STAMP_MS, 1795334648244, None)])
        state = coordinator_state.lookup(self.db, self.sessions_db, self.ids["t1"])
        self.assertEqual({
            "ticket_id": self.ids["t1"], "owner_session_id": "coordinator-1",
            "origin_session_id": "coordinator-1",
            "host_status": "archived", "host_source": "scheduler",
            "last_turn_ended_at": "2026-11-22T08:04:08.244000+00:00",
            "updated_at": "2026-11-22T08:04:08.266000+00:00",
            "verdict": "TAKEOVER_NEEDED",
        }, json.loads(json.dumps(state, default=str)))


if __name__ == "__main__":
    unittest.main()
