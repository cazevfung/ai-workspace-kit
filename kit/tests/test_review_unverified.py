"""AW-132：验收记录要有「未验」这一档，而不是逼人在 true / false 里二选一。

这份用例只对着库里的行为断言，不断言提示措辞：

1. **`review_result` 的 check 收三档**：`pass` / `unverified` / `fail`。`unverified`
   不阻断 `accept`，但要落得进库、读得出来、数得清——不是只能填 `true` 再在
   `reason` 里写「实为未验」。
2. **放宽的是「不许未验」，不是「失败也能过」**：`accept` 仍不许有 `fail`，
   `reject` 仍要求至少一条 `fail`。两条负例都在这里钉住。
3. **旧写法继续可用**：`{"id","passed","evidence"}` 收下并归一到 `status`，
   免得已有的调用方（含 `examples/demo.py`）一次性断掉。
4. **摘要分得开**：一张「全部已验证」的 accept 与一张「带未验项」的 accept，
   在 `get_ticket` 的工单级摘要上不是同一个字面，不必读 `reason` 才分得出来。
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from task_center import Center

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "89abcdef0123456789abcdef0123456789abcdef"
CRITERIA_ONE = [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}]
CRITERIA_TWO = [
    {"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"},
    {"id": "C2", "text": "含 `git status --short` 且输出为空"},
]


class ReviewUnverifiedBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "验收项目"
        self.project.mkdir()
        (self.project / "PRD.md").write_text("需求第一版", encoding="utf-8")
        self.db = self.root / "center.sqlite3"
        self.center = Center(self.db)
        self.counter = 0
        self.ids = {}
        self.ok("init")
        self.ok("register_project", {"project_id": "p1", "name": "验收项目", "root": str(self.project)})

    def call(self, op, payload=None, request_id=None):
        self.counter += 1
        return self.center.execute(op, payload or {}, request_id or f"req-{self.counter}")

    def ok(self, op, payload=None, request_id=None):
        result = self.call(op, payload, request_id)
        self.assertTrue(result["ok"], result)
        return result["result"]

    def error(self, code, op, payload=None, request_id=None):
        result = self.call(op, payload, request_id)
        self.assertFalse(result["ok"], result)
        self.assertEqual(code, result["error"]["code"], result)
        return result

    def tid(self, alias="t1"):
        return self.ids[alias]

    def scope(self, alias="t1", **extra):
        return {"project_id": "p1", "ticket_id": self.tid(alias), **extra}

    def submitted(self, alias="t1", criteria=None):
        """造一张待验收的单：开单 → 领取 → 绑定 → 提交，返回 (attempt_id, submission_id)。"""
        created = self.ok("create_ticket", {
            "project_id": "p1", "title": f"{alias} 的成果", "deliverable": "交一件能被验收的东西",
            "prd_path": "PRD.md", "criteria": criteria or CRITERIA_ONE})
        self.ids[alias] = created["id"]
        attempt = self.ok("claim_ticket", self.scope(
            alias, expected_revision=1, coordinator_session_id="coordinator-1"))["attempt_id"]
        self.ok("bind_session", self.scope(
            alias, attempt_id=attempt, session_id="executor-1", project_root=str(self.project)))
        (self.project / "结果.md").write_text("成果", encoding="utf-8")
        submitted = self.ok("submit_result", self.scope(
            alias, attempt_id=attempt, session_id="executor-1", summary="成果已完成",
            artifacts=["结果.md"], evidence=["跑过一遍"], risks=[],
            head_sha=HEAD_SHA, base_sha=BASE_SHA))
        return attempt, submitted["submission_id"]

    def review(self, attempt, submission, checks, verdict="accept", alias="t1", **extra):
        return self.call("review_result", self.scope(
            alias, attempt_id=attempt, submission_id=submission, reviewer_session_id="reviewer-1",
            verdict=verdict, checks=checks, reason="逐条核对后作出结论", **extra))

    def detail(self, alias="t1"):
        return self.ok("get_ticket", self.scope(alias))


class UnverifiedTierTest(ReviewUnverifiedBase):
    """C1：一条 review 能带「未验」档，且库里能按它统计。"""

    def test_accept_with_unverified_is_recorded_and_counted(self):
        attempt, submission = self.submitted()
        receipt = self.ok("review_result", self.scope(
            "t1", attempt_id=attempt, submission_id=submission, reviewer_session_id="reviewer-1",
            verdict="accept",
            checks=[{"id": "C1", "status": "unverified", "evidence": "设备签名装不上，真机判据无法执行"}],
            reason="唯一一条真机判据无法执行，按未验记录"))
        self.assertEqual("done", receipt["state"])
        self.assertEqual({"total": 1, "pass": 0, "unverified": 1, "fail": 0}, receipt["checks_summary"])
        self.assertIn("未验", receipt["summary"])

        detail = self.detail()
        self.assertEqual("done", detail["state"])
        check = detail["reviews"][0]["checks"][0]
        self.assertEqual("unverified", check["status"])
        # 未验既不是 true 也不是 false：不许留一个 `passed` 让人猜成后者。
        self.assertNotIn("passed", check)
        self.assertEqual(1, detail["reviews"][0]["checks_summary"]["unverified"])
        self.assertEqual(1, detail["unverified_checks"])

    def test_accept_still_forbids_a_failed_check(self):
        attempt, submission = self.submitted()
        result = self.error("INVALID_INPUT", "review_result", self.scope(
            "t1", attempt_id=attempt, submission_id=submission, reviewer_session_id="reviewer-1",
            verdict="accept",
            checks=[{"id": "C1", "status": "fail", "evidence": "用例没跑过"}],
            reason="想放过去"))
        self.assertIn("fail", result["error"]["message"])
        self.assertEqual("awaiting_review", self.detail()["state"])
        self.assertEqual([], self.detail()["reviews"])

    def test_reject_needs_a_failed_check(self):
        attempt, submission = self.submitted()
        # 未验不是失败：拿它当理由打回，等于「没验就能判不合格」。
        self.error("INVALID_INPUT", "review_result", self.scope(
            "t1", attempt_id=attempt, submission_id=submission, reviewer_session_id="reviewer-1",
            verdict="reject",
            checks=[{"id": "C1", "status": "unverified", "evidence": "没验"}],
            reason="没验所以打回"))
        self.error("INVALID_INPUT", "review_result", self.scope(
            "t1", attempt_id=attempt, submission_id=submission, reviewer_session_id="reviewer-1",
            verdict="reject",
            checks=[{"id": "C1", "status": "pass", "evidence": "验过了"}],
            reason="但还是打回"))
        self.assertEqual("awaiting_review", self.detail()["state"])


class LegacyCheckShapeTest(ReviewUnverifiedBase):
    """旧写法继续可用，落库时归一到 `status`。"""

    def test_passed_boolean_is_normalized(self):
        attempt, submission = self.submitted()
        receipt = self.ok("review_result", self.scope(
            "t1", attempt_id=attempt, submission_id=submission, reviewer_session_id="reviewer-1",
            verdict="accept",
            checks=[{"id": "C1", "passed": True, "evidence": "旧写法"}],
            reason="旧写法仍然通过"))
        self.assertEqual("done", receipt["state"])
        check = self.detail()["reviews"][0]["checks"][0]
        self.assertEqual("pass", check["status"])
        self.assertIs(True, check["passed"])

    def test_bad_status_shapes_are_rejected(self):
        attempt, submission = self.submitted()
        for bad in (
            [{"id": "C1", "status": "maybe", "evidence": "不认识"}],
            [{"id": "C1", "status": "pass", "passed": False, "evidence": "自相矛盾"}],
            [{"id": "C1", "status": "unverified", "passed": True, "evidence": "未验却说过"}],
            [{"id": "C1", "status": "pass"}],
        ):
            with self.subTest(checks=bad):
                self.error("INVALID_INPUT", "review_result", self.scope(
                    "t1", attempt_id=attempt, submission_id=submission, reviewer_session_id="reviewer-1",
                    verdict="accept", checks=bad, reason="形状不对"))


class ReviewSummaryTest(ReviewUnverifiedBase):
    """C2：带未验项的 accept 在工单摘要上分得出来。"""

    def test_summary_distinguishes_unverified_accept_from_fully_verified(self):
        verified_attempt, verified_sub = self.submitted("verified", CRITERIA_TWO)
        self.ok("review_result", self.scope(
            "verified", attempt_id=verified_attempt, submission_id=verified_sub,
            reviewer_session_id="reviewer-1", verdict="accept",
            checks=[{"id": "C1", "status": "pass", "evidence": "跑过，退出码 0"},
                    {"id": "C2", "status": "pass", "evidence": "工作区干净"}],
            reason="两条都验了"))
        partial_attempt, partial_sub = self.submitted("partial", CRITERIA_TWO)
        self.ok("review_result", self.scope(
            "partial", attempt_id=partial_attempt, submission_id=partial_sub,
            reviewer_session_id="reviewer-2", verdict="accept",
            checks=[{"id": "C1", "status": "pass", "evidence": "跑过，退出码 0"},
                    {"id": "C2", "status": "unverified", "evidence": "这台机器上没有可用的真机"}],
            reason="一条真机判据无法执行，按未验记录"))

        verified, partial = self.detail("verified"), self.detail("partial")
        # 两张单都是 done：状态逐字相同，所以差别只能落在摘要上。
        self.assertEqual(verified["state"], partial["state"])
        self.assertNotEqual(verified["review_summary"], partial["review_summary"])
        self.assertEqual(0, verified["unverified_checks"])
        self.assertEqual(1, partial["unverified_checks"])
        self.assertIn("未验", partial["review_summary"])
        self.assertNotIn("未验", verified["review_summary"])

    def test_legacy_stored_review_still_reads(self):
        """库里 pre-AW-132 的行只有 `passed`：`get_ticket` 要读得出来，不能抛异常。

        这是真实库的现场——历史 review 一条都不是新形状。所以读取侧补一层兼容，
        而不是要求「所有旧行都得迁移」。
        """
        attempt, submission = self.submitted()
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?,?)", (
                "legacy-review", submission, "reviewer-old", "accept",
                json.dumps([{"id": "C1", "passed": True, "evidence": "旧行只存了布尔"}]),
                "旧写法", "2026-11-20T00:00:00+00:00"))
            conn.commit()
        finally:
            conn.close()
        detail = self.detail()
        check = detail["reviews"][0]["checks"][0]
        self.assertEqual("pass", check["status"])
        self.assertEqual({"total": 1, "pass": 1, "unverified": 0, "fail": 0},
                         detail["reviews"][0]["checks_summary"])
        self.assertEqual(0, detail["unverified_checks"])

    def test_ticket_without_review_has_null_summary(self):
        created = self.ok("create_ticket", {
            "project_id": "p1", "title": "还没验收", "deliverable": "等着",
            "prd_path": "PRD.md", "criteria": CRITERIA_ONE})
        self.ids["fresh"] = created["id"]
        detail = self.detail("fresh")
        self.assertIsNone(detail["review_summary"])
        self.assertEqual(0, detail["unverified_checks"])


if __name__ == "__main__":
    unittest.main()
