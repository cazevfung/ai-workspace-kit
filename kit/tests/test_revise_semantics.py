"""schema 10：改判据不作废在飞的工作，每份提交说得出基于哪版 master（PRD-013 R6 / R7）。

这四条语义以前只有一条：`revise_ticket` 调用 `_invalidate`，改一次判据就把在飞的
尝试 supersede 掉、revision 加一。于是「判据变了」「这一代作废了」两件事被压成
一个动作，执行会话拿着已经过期的标准继续干活，而验收只看得到 revision 变了。

这里逐个钉住新的口径：改判据只推 `criteria_version`；作废只剩 `recover_ticket`、
`cancel_ticket` 与 `revise_ticket(restart=true)` 三个入口；`submit_result` 必须带
`base_sha`；`list_tickets` 的输出表头带 `criteria_v` 与 `behind`。

用例里的 sha 都是手写的合法 40 位十六进制串：本工具只校形状，不跑 git——
真正需要 git 的那一条（`behind` 的提交数）单独用临时仓库验。
"""
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from task_center import Center

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "89abcdef0123456789abcdef0123456789abcdef"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"

CRITERIA_V1 = [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}]
CRITERIA_V2 = [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"},
               {"id": "C2", "text": "判断类：清单是否带负责人栏；反例：只有标题没有负责人"}]


class CenterBase(unittest.TestCase):
    """所有用例共用的现场：一个临时项目、一个中心库、一次显式 init。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "中文项目"
        self.project.mkdir()
        (self.project / "PRD.md").write_text("需求第一版", encoding="utf-8")
        self.db = self.root / "center.sqlite3"
        self.center = Center(self.db)
        self.counter = 0
        # 库发号之后，测试里仍用 t1 / t2 指代工单；resolve() 发请求前换成库发的号。
        self.ids = {}
        self.ok("init")
        self.ok("register_project", {"project_id": "p1", "name": "项目一", "root": str(self.project)})

    def call(self, op, payload=None, request_id=None):
        self.counter += 1
        return self.center.execute(op, self.resolve(payload or {}), request_id or f"req-{self.counter}")

    def resolve(self, payload):
        """把 payload 里的短名换成库发的工单号（create_ticket 不再收自报号）。"""
        alias = payload.get("ticket_id")
        real = self.ids.get(alias) if isinstance(alias, str) else None
        return {**payload, "ticket_id": real} if real else payload

    def tid(self, alias="t1"):
        return self.ids[alias]

    def ok(self, op, payload=None, request_id=None):
        result = self.call(op, payload, request_id)
        self.assertTrue(result["ok"], result)
        return result["result"]

    def error(self, code, op, payload=None, request_id=None):
        result = self.call(op, payload, request_id)
        self.assertFalse(result["ok"], result)
        self.assertEqual(code, result["error"]["code"], result)
        return result

    def scope(self, **extra):
        return {"project_id": "p1", "ticket_id": "t1", **extra}

    def create(self, alias="t1", **extra):
        created = self.ok("create_ticket", {
            "project_id": "p1", "title": "整理检查清单",
            "deliverable": "一份逐项可勾选的检查清单", "prd_path": "PRD.md",
            "criteria": CRITERIA_V1, **extra})
        self.ids[alias] = created["id"]
        return created

    def ticket(self):
        return self.create("t1")

    def running(self, session="executor-1"):
        claim = self.ok("claim_ticket", self.scope(expected_revision=1, coordinator_session_id="coordinator-1"))
        self.ok("bind_session", self.scope(attempt_id=claim["attempt_id"], session_id=session,
                                          project_root=str(self.project)))
        return claim["attempt_id"]

    def result_payload(self, attempt, session="executor-1"):
        (self.project / "result.md").write_text("1. 检查步骤", encoding="utf-8")
        return self.scope(attempt_id=attempt, session_id=session, artifacts=["result.md"],
                          summary="清单已完成", evidence=["人工检查条目"], risks=[],
                          head_sha=HEAD_SHA, base_sha=BASE_SHA)

    def revise(self, **extra):
        payload = self.scope(expected_revision=1, reason="补一条验收条件", prd_path="PRD.md",
                             criteria=CRITERIA_V2)
        return self.call("revise_ticket", {**payload, **extra})

class ReviseSemanticsTest(CenterBase):
    # --- C3：改判据不作废在飞尝试 -----------------------------------------

    def test_revise_keeps_the_in_flight_attempt_alive(self):
        """改判据只推 criteria_version：尝试仍是 running，current_attempt 与 revision 都不动。"""
        self.ticket()
        attempt = self.running()
        receipt = self.revise()
        self.assertTrue(receipt["ok"], receipt)
        result = receipt["result"]
        self.assertEqual("running", result["state"])
        self.assertEqual(1, result["revision"])
        self.assertEqual({"from": 1, "to": 2}, result["criteria_version"])

        ticket = self.ok("get_ticket", self.scope())
        self.assertEqual("running", ticket["state"])
        self.assertEqual(1, ticket["revision"])
        self.assertEqual(2, ticket["criteria_version"])
        self.assertEqual(attempt, ticket["current_attempt"])
        self.assertEqual(["running"], [a["state"] for a in ticket["attempts"]])
        # 尝试记的是它按哪一版判据开始的，revise 不追改它（否则「提交时判据变了」判不出来）。
        self.assertEqual(1, ticket["attempts"][0]["criteria_version"])

    def test_revise_reports_must_wake_and_records_the_event(self):
        """在飞时回执带 must_wake 三样，并落一条 criteria_revised_in_flight。"""
        self.ticket()
        attempt = self.running()
        receipt = self.revise()
        must_wake = receipt["result"]["must_wake"]
        self.assertIsNotNone(must_wake, receipt)
        self.assertEqual("executor-1", must_wake["session_id"])
        self.assertEqual(attempt, must_wake["attempt_id"])
        self.assertEqual({"from": 1, "to": 2}, must_wake["criteria_version"])

        events = self.ok("events", self.scope())["items"]
        revised = [item for item in events if item["operation"] == "criteria_revised_in_flight"]
        self.assertEqual(1, len(revised), events)
        detail = revised[0]["detail"]
        self.assertEqual("executor-1", detail["session_id"])
        self.assertEqual(attempt, detail["attempt_id"])
        self.assertEqual({"from": 1, "to": 2}, detail["criteria_version"])

    def test_revise_never_touches_attempts_that_are_done(self):
        """已终结的尝试（accepted / rejected）本来就是审计真相，revise 更不该动它。"""
        self.ticket()
        attempt = self.running()
        submission = self.ok("submit_result", self.result_payload(attempt))
        self.ok("review_result", self.scope(attempt_id=attempt, submission_id=submission["submission_id"],
                                           reviewer_session_id="reviewer-1", verdict="accept",
                                           checks=[{"id": "C1", "passed": True, "evidence": "逐项核对"}],
                                           reason="逐项检查后作出结论"))
        receipt = self.revise(reopen=True)
        self.assertTrue(receipt["ok"], receipt)
        ticket = self.ok("get_ticket", self.scope())
        self.assertEqual("accepted", ticket["attempts"][0]["state"])
        self.assertEqual("ready", ticket["state"])

    # --- C4：作废只留三个入口 ---------------------------------------------

    def test_restart_true_still_invalidates(self):
        """显式 restart=true 才走老路：尝试 superseded、revision+1、回到 ready，没有 must_wake。"""
        self.ticket()
        attempt = self.running()
        receipt = self.revise(restart=True)
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual("ready", receipt["result"]["state"])
        self.assertEqual(2, receipt["result"]["revision"])
        self.assertIsNone(receipt["result"]["must_wake"])
        ticket = self.ok("get_ticket", self.scope())
        self.assertIsNone(ticket["current_attempt"])
        self.assertEqual("superseded", ticket["attempts"][0]["state"])
        self.assertEqual("ready", ticket["state"])

    def test_restart_must_be_a_boolean(self):
        self.ticket()
        self.running()
        for bad in ("true", 1, None, []):
            with self.subTest(restart=bad):
                self.error("INVALID_INPUT", "revise_ticket", {**self.scope(
                    expected_revision=1, reason="补一条验收条件", prd_path="PRD.md", criteria=CRITERIA_V2),
                    "restart": bad})

    def test_recover_and_cancel_still_invalidate(self):
        """另外两个入口各让原尝试 superseded、revision 加一。"""
        self.ticket()
        attempt = self.running()
        recovered = self.ok("recover_ticket", self.scope(expected_revision=1, attempt_id=attempt,
                                                        reason="已核对旧会话与实际产物"))
        self.assertEqual(2, recovered["revision"])
        self.assertEqual("superseded", self.ok("get_ticket", self.scope())["attempts"][0]["state"])

        self.ticket2 = self.create("t2", title="第二张", deliverable="第二份清单")
        claim = self.ok("claim_ticket", self.scope(
            ticket_id="t2", expected_revision=1, coordinator_session_id="coordinator-1",
            allow_parallel=True, parallel_reason="验的是取消这个入口，不是项目串行"))
        self.ok("bind_session", self.scope(ticket_id="t2", attempt_id=claim["attempt_id"],
                                          session_id="executor-2", project_root=str(self.project)))
        cancelled = self.ok("cancel_ticket", self.scope(ticket_id="t2", expected_revision=1, reason="用户取消"))
        self.assertEqual(2, cancelled["revision"])
        detail = self.ok("get_ticket", self.scope(ticket_id="t2"))
        self.assertEqual("superseded", detail["attempts"][0]["state"])
        self.assertIsNone(detail["current_attempt"])

    # --- C5 / AC12：提交必须带基座身份 -------------------------------------

    def test_submit_requires_base_sha(self):
        self.ticket()
        attempt = self.running()
        payload = self.result_payload(attempt)
        without = {key: value for key, value in payload.items() if key != "base_sha"}
        self.error("INVALID_INPUT", "submit_result", without)
        # 被拒时一行不写：提交条数不变，工单还停在 running。
        self.assertEqual([], self.ok("get_ticket", self.scope())["submissions"])
        self.assertEqual("running", self.ok("get_ticket", self.scope())["state"])
        receipt = self.ok("submit_result", payload)
        self.assertEqual(BASE_SHA, receipt["base_sha"])
        self.assertEqual(BASE_SHA, self.ok("get_ticket", self.scope())["submissions"][-1]["base_sha"])

    def test_submit_base_sha_shape_is_checked(self):
        self.ticket()
        attempt = self.running()
        payload = self.result_payload(attempt)
        for bad in (None, "", "   ", BASE_SHA[:39], BASE_SHA + "0", "Z" * 40, 12345, [BASE_SHA]):
            with self.subTest(base_sha=bad):
                body = {key: value for key, value in payload.items() if key != "base_sha"}
                if bad is not None:
                    body["base_sha"] = bad
                self.error("INVALID_INPUT", "submit_result", body)
        self.assertEqual("running", self.ok("get_ticket", self.scope())["state"])

    def test_submit_reports_criteria_changed(self):
        """改了判据之后交的成果：回执说清「这次是按旧版判据做的」。"""
        self.ticket()
        attempt = self.running()
        unchanged = self.ok("submit_result", self.result_payload(attempt))
        self.assertFalse(unchanged["criteria_changed"])

        self.create("t2", title="第二张", deliverable="第二份清单")
        claim = self.ok("claim_ticket", self.scope(
            ticket_id="t2", expected_revision=1, coordinator_session_id="coordinator-1",
            allow_parallel=True, parallel_reason="验的是判据变化，不是项目串行"))
        self.ok("bind_session", self.scope(ticket_id="t2", attempt_id=claim["attempt_id"],
                                          session_id="executor-2", project_root=str(self.project)))
        self.assertTrue(self.call("revise_ticket", self.scope(
            ticket_id="t2", expected_revision=1, reason="补一条验收条件",
            prd_path="PRD.md", criteria=CRITERIA_V2))["ok"])
        (self.project / "result-2.md").write_text("1. 检查步骤", encoding="utf-8")
        late = self.ok("submit_result", self.scope(
            ticket_id="t2", attempt_id=claim["attempt_id"], session_id="executor-2",
            artifacts=["result-2.md"], summary="清单已完成", evidence=["人工检查条目"], risks=[],
            head_sha=HEAD_SHA, base_sha=BASE_SHA))
        self.assertTrue(late["criteria_changed"], late)

    # --- C6 / AC12：list_tickets 的 criteria_v 与 behind ------------------

    def test_list_tickets_header_carries_criteria_v_and_behind(self):
        self.ticket()
        listed = self.ok("list_tickets")
        self.assertIn("criteria_v", listed["columns"], listed)
        self.assertIn("behind", listed["columns"], listed)
        item = listed["items"][0]
        self.assertEqual(1, item["criteria_v"])
        # 不是 git 仓库：算不出来，写 ? 而不是 0——0 会和「基于最新 master」分不开。
        self.assertEqual("?", item["behind"])

    def test_list_tickets_hides_behind_for_legacy_submissions(self):
        """没有 head_sha 的历史提交（schema 9 之前）同样算不出来，只能是 ?。"""
        self.ticket()
        attempt = self.running()
        self.ok("submit_result", self.result_payload(attempt))
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE submissions SET head_sha=NULL")
            conn.commit()
        finally:
            conn.close()
        item = self.ok("list_tickets")["items"][0]
        self.assertEqual("?", item["behind"])


@unittest.skipUnless(shutil.which("git"), "需要 git 才能算 behind")
class BehindCountTest(CenterBase):
    """behind 是「目标分支上有、这份提交里没有」的提交数：只在这一条里真的跑 git。"""

    def git(self, *args):
        process = subprocess.run(["git", "-C", str(self.project), *args], capture_output=True,
                                 text=True, encoding="utf-8", timeout=60)
        self.assertEqual(0, process.returncode, process.stderr)
        return process.stdout.strip()

    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.git("config", "user.email", "executor@example.com")
        self.git("config", "user.name", "executor")
        (self.project / "PRD.md").write_text("需求第一版", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "第一版需求")
        self.git("branch", "-M", "master")

    def test_behind_counts_commits_the_submission_does_not_have(self):
        base = self.git("rev-parse", "HEAD")
        self.ticket()
        attempt = self.running()
        submission = self.ok("submit_result", {**self.result_payload(attempt), "head_sha": base,
                                               "base_sha": base})
        self.assertEqual(base, submission["head_sha"])
        self.assertEqual(0, self.ok("list_tickets")["items"][0]["behind"])
        (self.project / "extra.md").write_text("后来的一条提交", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "master 又往前走了一条")
        self.assertEqual(1, self.ok("list_tickets")["items"][0]["behind"])


if __name__ == "__main__":
    unittest.main()
