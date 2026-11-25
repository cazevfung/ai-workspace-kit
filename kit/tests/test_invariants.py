"""不变量自检的用例：三类可疑行各要能抓到，干净库一条都不许报（AW-159）。

这张单真正抓得到 HSE-005 那条 `ae87a2fc` 的东西就是这个自检，不是用例——工具层
根本没有产出那种行的路径（`INSERT INTO attempts` 全仓只有 `_claim_ticket` 一处，
而它每写一次都会由 `execute()` 落一条 `claim_ticket` 事件）。所以用例要证明的是
「自检真的会红」：每条都先在临时库里造一条绕开工具的行，再断言检查器点得出它。

三类各自的最小证据：

1. `attempt_without_claim`：一条 attempts 行，没有 claim 事件指着它；
2. `revision_without_event`：`UPDATE tickets SET revision=revision+1`，没有任何操作；
3. `artifact_key_shape`：artifacts 里多一个键、少一个键、或整项是个字符串。

跑法：

    python -m unittest tests.test_invariants -v
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from task_center import Center, invariants

ROOT = Path(__file__).resolve().parent.parent
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "89abcdef0123456789abcdef0123456789abcdef"


class InvariantsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "中文项目"
        self.project.mkdir()
        (self.project / "PRD.md").write_text("需求第一版", encoding="utf-8")
        (self.project / "result.md").write_text("1. 检查步骤", encoding="utf-8")
        self.db = self.root / "center.sqlite3"
        self.center = Center(self.db)
        self.counter = 0
        self.ok("init")
        self.ok("register_project", {"project_id": "p1", "name": "项目一", "root": str(self.project)})

    # --- 夹具 -------------------------------------------------------------

    def call(self, op, payload=None, request_id=None):
        self.counter += 1
        return self.center.execute(op, payload or {}, request_id or f"req-{self.counter}")

    def ok(self, op, payload=None, request_id=None):
        result = self.call(op, payload, request_id)
        self.assertTrue(result["ok"], result)
        return result["result"]

    def raw(self, sql, params=()):
        """绕开工具直接写库——自检要抓的就是这种行。写完立刻关连接，不占着写锁。"""
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(sql, params).fetchall()
            conn.commit()
        finally:
            conn.close()
        return rows

    def happy_ticket(self):
        """一张走完整条工具链的单：claim → bind → submit → accept。"""
        created = self.ok("create_ticket", {
            "project_id": "p1", "title": "整理检查清单",
            "deliverable": "一份逐项可勾选的检查清单", "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        })
        ticket_id = created["id"]
        scope = {"project_id": "p1", "ticket_id": ticket_id}
        attempt = self.ok("claim_ticket", {**scope, "expected_revision": 1,
                                           "coordinator_session_id": "coordinator-1"})["attempt_id"]
        self.ok("bind_session", {**scope, "attempt_id": attempt, "session_id": "executor-1",
                                 "project_root": str(self.project)})
        submission = self.ok("submit_result", {
            **scope, "attempt_id": attempt, "session_id": "executor-1", "artifacts": ["result.md"],
            "summary": "清单已完成", "evidence": ["人工检查条目"], "risks": [],
            "head_sha": HEAD_SHA, "base_sha": BASE_SHA})["submission_id"]
        self.ok("review_result", {
            **scope, "attempt_id": attempt, "submission_id": submission,
            "reviewer_session_id": "reviewer-1", "verdict": "accept",
            "checks": [{"id": "C1", "status": "pass", "evidence": "跑了一遍，全绿"}],
            "reason": "按判据核过"})
        return ticket_id, attempt, submission

    def report(self, project_id=None):
        conn = sqlite3.connect(self.db)
        try:
            conn.row_factory = sqlite3.Row
            return invariants.check(conn, project_id)
        finally:
            conn.close()

    # --- 干净库一条都不许报 -----------------------------------------------

    def test_a_clean_ticket_reports_nothing(self):
        """反例守一手：走完整条工具链的单，三类都是 0。误报会把自检变成背景噪音。"""
        self.happy_ticket()
        report = self.report()
        self.assertTrue(report["ok"], report["findings"])
        self.assertEqual([], report["findings"])
        self.assertEqual({name: 0 for name in invariants.CHECKS}, report["counts"])

    def test_rejected_writes_do_not_count_as_suspicious(self):
        """被拒的调用会留一条 `{"rejected": ...}` 事件：它不是来源，也不算可疑。"""
        ticket_id, _, _ = self.happy_ticket()
        rejected = self.call("claim_ticket", {"project_id": "p1", "ticket_id": ticket_id,
                                             "expected_revision": 1,
                                             "coordinator_session_id": "coordinator-2"})
        self.assertFalse(rejected["ok"], rejected)
        # 拒收事件带着 `{"rejected": ...}`、没有 `result.attempt_id`：它不能被当成一条来源。
        self.assertEqual("INVALID_STATE", rejected["error"]["code"], rejected)
        self.assertTrue(self.report()["ok"], self.report()["findings"])

    # --- 第一类：有尝试、没有 claim 事件 ---------------------------------

    def test_attempt_without_claim_event_is_reported(self):
        """HSE-005 的 ae87a2fc：一条 revision 与工单对得上、却没人宣告过的尝试。"""
        ticket_id, attempt, _ = self.happy_ticket()
        ghost = "ae87a2fc-c57d-43db-b23c-fef16d8d4a9c"
        self.raw("INSERT INTO attempts(id,ticket_id,revision,criteria_version,prd_path,prd_sha256,state,"
                 "session_id,coordinator_session_id,coordinator_workdir,created_at,batch_key)"
                 " SELECT ?,ticket_id,revision,criteria_version,prd_path,prd_sha256,'running',"
                 "session_id,coordinator_session_id,coordinator_workdir,'2026-11-24T14:28:38.785497+00:00',NULL"
                 " FROM attempts WHERE id=?", (ghost, attempt))
        self.raw("UPDATE tickets SET current_attempt=?,state='running' WHERE id=?", (ghost, ticket_id))
        report = self.report()
        self.assertFalse(report["ok"])
        found = [item for item in report["findings"] if item["check"] == invariants.ATTEMPT_WITHOUT_CLAIM]
        self.assertEqual(1, len(found), report["findings"])
        self.assertEqual(ghost, found[0]["attempt_id"])
        self.assertEqual(ticket_id, found[0]["ticket_id"])
        # 已经有 claim 事件的那条尝试不该被一起点名。
        self.assertNotIn(attempt, [item.get("attempt_id") for item in report["findings"]])
        self.assertEqual(0, report["counts"][invariants.REVISION_WITHOUT_EVENT])

    # --- 第二类：revision 无事件地推高 ------------------------------------

    def test_revision_bumped_without_an_event_is_reported(self):
        ticket_id, _, _ = self.happy_ticket()
        self.raw("UPDATE tickets SET revision=revision+1 WHERE id=?", (ticket_id,))
        report = self.report()
        self.assertFalse(report["ok"])
        found = [item for item in report["findings"] if item["check"] == invariants.REVISION_WITHOUT_EVENT]
        self.assertEqual(1, len(found), report["findings"])
        self.assertEqual(ticket_id, found[0]["ticket_id"])
        self.assertEqual(2, found[0]["ticket_revision"])
        self.assertEqual(1, found[0]["event_revision"])
        # 报错要说清「多出来的那一次递增没有对应操作」，而不是只给两个数。
        self.assertIn("没有对应操作", found[0]["detail"])

    def test_cancelled_ticket_with_a_stray_attempt_is_reported(self):
        """把 HSE-005 的现场原样复刻一遍：先取消，再手写一条尝试。"""
        created = self.ok("create_ticket", {
            "project_id": "p1", "title": "整理检查清单",
            "deliverable": "一份逐项可勾选的检查清单", "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        })
        ticket_id = created["id"]
        scope = {"project_id": "p1", "ticket_id": ticket_id}
        attempt = self.ok("claim_ticket", {**scope, "expected_revision": 1,
                                           "coordinator_session_id": "coordinator-1"})["attempt_id"]
        self.ok("bind_session", {**scope, "attempt_id": attempt, "session_id": "executor-1",
                                 "project_root": str(self.project)})
        self.ok("cancel_ticket", {**scope, "expected_revision": 1, "reason": "用户取消"})
        # 取消之后的三条续做入口都过不去，所以下面这几行只能是绕开工具写进去的。
        for op, payload in (
                ("claim_ticket", {**scope, "expected_revision": 2, "coordinator_session_id": "c-2"}),
                ("submit_result", {**scope, "attempt_id": attempt, "session_id": "executor-1",
                                   "artifacts": ["result.md"], "summary": "清单已完成",
                                   "evidence": ["人工检查条目"], "risks": [],
                                   "head_sha": HEAD_SHA, "base_sha": BASE_SHA}),
                ("recover_ticket", {**scope, "attempt_id": attempt, "expected_revision": 2,
                                    "reason": "退回去"})):
            self.assertEqual("TICKET_CANCELLED", self.call(op, payload)["error"]["code"])
        ghost = "ae87a2fc-c57d-43db-b23c-fef16d8d4a9c"
        self.raw("INSERT INTO attempts(id,ticket_id,revision,criteria_version,prd_path,prd_sha256,state,"
                 "session_id,coordinator_session_id,coordinator_workdir,created_at,batch_key)"
                 " SELECT ?,ticket_id,revision,criteria_version,prd_path,prd_sha256,'running',"
                 "session_id,coordinator_session_id,coordinator_workdir,'2026-11-24T14:28:38.785497+00:00',NULL"
                 " FROM attempts WHERE id=?", (ghost, attempt))
        self.raw("UPDATE tickets SET current_attempt=?,state='running' WHERE id=?", (ghost, ticket_id))
        report = self.report()
        self.assertIn(ghost, [item.get("attempt_id") for item in report["findings"]], report["findings"])

    # --- 第三类：artifacts 键集合 ------------------------------------------

    def test_artifact_with_extra_key_is_reported(self):
        """HSE-008 的 1cd60777：唯一一条多出 `role` 的提交。"""
        _, _, submission = self.happy_ticket()
        items = json.loads(self.raw("SELECT artifacts FROM submissions WHERE id=?", (submission,))[0][0])
        self.raw("UPDATE submissions SET artifacts=? WHERE id=?",
                 (json.dumps([{**item, "role": "机制.md"} for item in items]), submission))
        report = self.report()
        found = [item for item in report["findings"] if item["check"] == invariants.ARTIFACT_KEY_SHAPE]
        self.assertEqual(1, len(found), report["findings"])
        self.assertEqual(submission, found[0]["submission_id"])
        self.assertEqual(0, found[0]["artifacts_index"])
        self.assertEqual(["role"], found[0]["extra_keys"])

    def test_artifact_that_is_a_bare_path_string_is_reported(self):
        """HSE-007 的 6aa4dadf：artifacts 直接是一串路径字符串（11 项）。"""
        _, _, submission = self.happy_ticket()
        self.raw("UPDATE submissions SET artifacts=? WHERE id=?",
                 (json.dumps(["result.md"]), submission))
        report = self.report()
        found = [item for item in report["findings"] if item["check"] == invariants.ARTIFACT_KEY_SHAPE]
        self.assertEqual(1, len(found), report["findings"])
        self.assertIn("str", found[0]["detail"])

    # --- 项目过滤与命令行退出码 -------------------------------------------

    def test_project_filter_narrows_the_scan(self):
        ticket_id, _, _ = self.happy_ticket()
        other = self.root / "另一个项目"
        other.mkdir()
        (other / "PRD.md").write_text("需求", encoding="utf-8")
        self.ok("register_project", {"project_id": "p2", "name": "项目二", "root": str(other)})
        self.raw("UPDATE tickets SET revision=revision+1 WHERE id=?", (ticket_id,))
        self.assertTrue(self.report("p2")["ok"], self.report("p2")["findings"])
        self.assertFalse(self.report("p1")["ok"])

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "tools" / "invariants.py"),
                               "--db", str(self.db), *args],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")

    def test_cli_exit_codes_say_clean_suspicious_and_unreadable(self):
        self.happy_ticket()
        clean = self.run_cli()
        self.assertEqual(0, clean.returncode, clean.stdout + clean.stderr)
        self.assertIn("全部干净", clean.stdout)
        ticket_id = self.raw("SELECT id FROM tickets")[0][0]
        self.raw("UPDATE tickets SET revision=revision+1 WHERE id=?", (ticket_id,))
        suspicious = self.run_cli()
        self.assertEqual(1, suspicious.returncode, suspicious.stdout)
        self.assertIn(ticket_id, suspicious.stdout)
        self.assertIn("revision_without_event", suspicious.stdout)
        unreadable = subprocess.run([sys.executable, str(ROOT / "tools" / "invariants.py"),
                                     "--db", str(self.root / "没有这个库.sqlite3")],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(2, unreadable.returncode)
        self.assertIn("库不存在", unreadable.stderr)

    def test_cli_refuses_a_database_that_is_not_a_center(self):
        """库能打开但不是中心库：要说「没法检」，不能说「检完了没可疑行」。"""
        foreign = self.root / "foreign.sqlite3"
        conn = sqlite3.connect(foreign)
        conn.execute("CREATE TABLE unrelated(value TEXT)")
        conn.commit()
        conn.close()
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "invariants.py"), "--db", str(foreign)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(2, result.returncode, result.stdout)
        self.assertIn("不是「检完干净」", result.stderr)

    def test_cli_speaks_utf8_even_when_nobody_sets_pythonioencoding(self):
        """没人替子进程设 `PYTHONIOENCODING` 时也要判得准（AW-164）。

        `python tools/run_tests.py` 会往子进程 env 里塞 `PYTHONIOENCODING=utf-8`
        （`tools/run_tests.py:297`），所以这条用例以前只在那个入口下绿：编码是环境
        凑巧给的，不是 CLI 自己定的。这里把它从 env 里摘掉，等价于在 Windows 控制台
        直接敲 `python -m unittest tests.test_invariants`——CLI 必须自己按 utf-8 输出
        （`tools/invariants.py` 的 `main()` 一进来就 reconfigure），测试侧才解得出中文。
        """
        self.happy_ticket()
        env = {key: value for key, value in os.environ.items() if key != "PYTHONIOENCODING"}
        script = str(ROOT / "tools" / "invariants.py")
        clean = subprocess.run([sys.executable, script, "--db", str(self.db)],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", env=env)
        self.assertEqual(0, clean.returncode, clean.stdout + clean.stderr)
        self.assertIn("全部干净", clean.stdout, clean.stdout)
        missing = subprocess.run([sys.executable, script, "--db", str(self.root / "没有这个库.sqlite3")],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", env=env)
        self.assertEqual(2, missing.returncode, missing.stderr)
        self.assertIn("库不存在", missing.stderr, missing.stderr)

    def test_cli_json_output_is_machine_readable(self):
        ticket_id, _, _ = self.happy_ticket()
        self.raw("UPDATE tickets SET revision=revision+1 WHERE id=?", (ticket_id,))
        result = self.run_cli("--json")
        self.assertEqual(1, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(1, payload["counts"][invariants.REVISION_WITHOUT_EVENT])
        self.assertEqual(ticket_id, payload["findings"][0]["ticket_id"])


if __name__ == "__main__":
    unittest.main()
