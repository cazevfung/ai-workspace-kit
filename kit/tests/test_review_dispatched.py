"""AW-133：`review_dispatched` 只落一条可判的事实——「验收已经派出去了」。

它补的是 `tools/next_action.py` 缺的那一半：`awaiting_review` + 没有 `reviews` 行
这两个条件在验收会话跑的那几分钟里一直成立，于是同一张单被连着几轮重复派验收。
派出去的那一刻要留下一条可查的事实，读侧才有东西可判。

这一批用例判四件事：

1. **只落事件，不动状态机。** `state` 还是 `awaiting_review`、`revision` 不变、
   `reviews` 表一行不写；事件正文里带 `submission_id`（读侧靠它认出「派的是哪一份成果」）。
2. **判序按「这张单现在能不能被派验收」排队。** 状态不对 / 没有提交 / 提交不是最新那条 /
   已经有验收结论，四种都被拒，且一行不写。
3. **接收执里能读出窗口。** `in_flight_until` 是派出去那一刻 + 30 分钟（窗口只有一个来源：
   `task_center.core.REVIEW_IN_FLIGHT_SECONDS`）。
4. **拒绝也留痕**：被拒时 `events` 里有一条带 `rejected` 的行，读侧不许把它当成「已派」。

`reviewer_session_id` 与 `note` 可给可不给——它们只是给人看的指向，不是身份认证。
"""
import tempfile
import unittest
from pathlib import Path

from task_center import Center
from task_center.core import REVIEW_IN_FLIGHT_SECONDS

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "89abcdef0123456789abcdef0123456789abcdef"


class ReviewDispatchedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "项目一"
        self.project.mkdir()
        (self.project / "PRD.md").write_text("需求第一版", encoding="utf-8")
        self.db = self.root / "center.sqlite3"
        self.center = Center(self.db)
        self.counter = 0
        self.ids = {}
        self.ok("init")
        self.ok("register_project", {"project_id": "p1", "name": "项目一",
                                     "root": str(self.project)})

    # --- 夹具 -------------------------------------------------------------

    def call(self, op, payload=None, request_id=None):
        self.counter += 1
        return self.center.execute(op, self.resolve(payload or {}),
                                   request_id or f"req-{self.counter}")

    def resolve(self, payload):
        alias = payload.get("ticket_id")
        real = self.ids.get(alias) if isinstance(alias, str) else None
        return {**payload, "ticket_id": real} if real else payload

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

    def ticket(self):
        created = self.ok("create_ticket", {
            "project_id": "p1", "title": "整理检查清单",
            "deliverable": "一份逐项可勾选的检查清单",
            "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        })
        self.ids["t1"] = created["id"]
        return created

    def claim(self):
        """按库里的当前代次领单：返工之后 revision 会变，写死 1 只会得到 STALE_REVISION。"""
        revision = self.ok("get_ticket", self.scope())["revision"]
        return self.ok("claim_ticket", self.scope(
            expected_revision=revision, coordinator_session_id="coordinator-1"))

    def submit(self, summary="清单已完成", head_sha=HEAD_SHA):
        claim = self.claim()
        self.ok("bind_session", self.scope(attempt_id=claim["attempt_id"],
                                           session_id="executor-1",
                                           project_root=str(self.project)))
        (self.project / "result.md").write_text("1. 检查步骤", encoding="utf-8")
        return self.ok("submit_result", {
            **self.scope(), "attempt_id": claim["attempt_id"], "session_id": "executor-1",
            "artifacts": ["result.md"], "summary": summary,
            "evidence": ["人工检查条目"], "risks": [],
            "head_sha": head_sha, "base_sha": BASE_SHA,
        })

    def awaiting_review(self):
        """一张正在等验收的单：提交已落库，`reviews` 还是空的。"""
        self.ticket()
        submission = self.submit()
        return submission["submission_id"]

    def accepted_events(self):
        """成功那次的事件：`execute()` 写的行带 `result`，被拒的行带 `rejected`。"""
        return [row for row in self.events() if "result" in (row["detail"] or {})]

    def mark(self, submission_id, **extra):
        return self.ok("review_dispatched", self.scope(submission_id=submission_id, **extra))

    def events(self, operation="review_dispatched"):
        rows = self.ok("events", self.scope(limit=100))["items"]
        return [row for row in rows if row["operation"] == operation]

    # --- 成功路径 ---------------------------------------------------------

    def test_marks_without_touching_the_ticket(self):
        submission_id = self.awaiting_review()
        before = self.ok("get_ticket", self.scope())
        receipt = self.mark(submission_id, reviewer_session_id="reviewer-1",
                            note="派给新开的验收会话")
        self.assertEqual(before["id"], receipt["ticket_id"])
        self.assertEqual("awaiting_review", receipt["state"])
        self.assertEqual(before["revision"], receipt["revision"])
        self.assertEqual(submission_id, receipt["submission_id"])
        self.assertEqual("reviewer-1", receipt["reviewer_session_id"])
        after = self.ok("get_ticket", self.scope())
        self.assertEqual("awaiting_review", after["state"])
        self.assertEqual(before["revision"], after["revision"])
        # 验收结论只有验收会话能写：这条事件不是结论。
        self.assertEqual([], after["reviews"])

    def test_the_event_is_what_the_planner_reads(self):
        submission_id = self.awaiting_review()
        self.mark(submission_id, reviewer_session_id="reviewer-1")
        rows = self.events()
        self.assertEqual(1, len(rows))
        detail = rows[0]["detail"]["result"]
        self.assertEqual(submission_id, detail["submission_id"])
        self.assertEqual("reviewer-1", detail["reviewer_session_id"])
        self.assertTrue(rows[0]["created_at"])

    def test_the_receipt_carries_the_window(self):
        submission_id = self.awaiting_review()
        receipt = self.mark(submission_id)
        self.assertIsNone(receipt["reviewer_session_id"])
        self.assertIn("in_flight_until", receipt)
        # 窗口只有一个来源；写在这里只是给人读，读侧的判据仍是事件时间 + 同一个常量。
        from datetime import datetime, timedelta, timezone
        until = datetime.fromisoformat(receipt["in_flight_until"])
        now = datetime.now(timezone.utc)
        delta = until - now
        self.assertGreater(delta, timedelta(seconds=REVIEW_IN_FLIGHT_SECONDS - 60))
        self.assertLessEqual(delta, timedelta(seconds=REVIEW_IN_FLIGHT_SECONDS))

    # --- 拒绝路径：一行不写 -----------------------------------------------

    def test_a_ticket_not_waiting_for_review_is_refused(self):
        self.ticket()
        self.claim()
        self.error("INVALID_STATE", "review_dispatched", self.scope(submission_id="sub-x"))
        self.assertEqual([], self.accepted_events())
        # 拒绝本身仍留一行带 rejected 的事件：它不是「已派」。
        rejected = [row for row in self.events()
                    if "rejected" in (row["detail"] or {})]
        self.assertEqual(1, len(rejected))

    def test_a_ticket_without_submission_is_refused(self):
        self.ticket()
        self.claim()
        self.error("INVALID_STATE", "review_dispatched", self.scope(submission_id="sub-x"))

    def test_only_the_latest_submission_can_be_marked(self):
        """返工后的新提交还没人看过：拿旧提交去标「已派」会把新提交压住。"""
        submission_id = self.awaiting_review()
        # 返工：recover → ready → 再领一次 → 新提交。
        state = self.ok("get_ticket", self.scope())
        self.ok("recover_ticket", self.scope(attempt_id=state["current_attempt"],
                                            expected_revision=state["revision"],
                                            reason="验收前先返工改一版"))
        new = self.submit(summary="第二版清单", head_sha="f" * 40)
        self.assertNotEqual(submission_id, new["submission_id"])
        self.error("STALE_SUBMISSION", "review_dispatched",
                   self.scope(submission_id=submission_id))
        self.assertEqual([], self.accepted_events())

    def test_a_submission_that_already_has_a_review_is_refused(self):
        submission_id = self.awaiting_review()
        self.ok("review_result", {
            **self.scope(), "attempt_id": self.ok("get_ticket", self.scope())["current_attempt"],
            "submission_id": submission_id, "reviewer_session_id": "reviewer-1",
            "verdict": "reject",
            "checks": [{"id": "C1", "status": "fail", "evidence": "清单缺第二项"}],
            "reason": "按条件逐条判，有一条不过",
        })
        # reject 之后单回到 rework，再走一遍：状态先拒。
        self.error("INVALID_STATE", "review_dispatched", self.scope(submission_id=submission_id))

    def test_a_reviewed_submission_still_awaiting_review_is_refused(self):
        """状态机慢一步的现场：提交已有结论、单还停在 awaiting_review，不许再标「已派」。

        这一格是脏行（`review_result` 本会把单带出 `awaiting_review`），用例直接把它
        扳回去造现场——要防的正是「库里两种说法打架时，有人又派了一个验收人」。
        """
        import sqlite3

        submission_id = self.awaiting_review()
        self.ok("review_result", {
            **self.scope(), "attempt_id": self.ok("get_ticket", self.scope())["current_attempt"],
            "submission_id": submission_id, "reviewer_session_id": "reviewer-1",
            "verdict": "accept",
            "checks": [{"id": "C1", "status": "pass", "evidence": "清单逐项对照过"}],
            "reason": "逐项检查后作出结论",
        })
        self.assertEqual("done", self.ok("get_ticket", self.scope())["state"])
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute("UPDATE tickets SET state='awaiting_review' WHERE id=?",
                         (self.ids["t1"],))
            conn.commit()
        finally:
            conn.close()
        self.error("ALREADY_REVIEWED", "review_dispatched", self.scope(submission_id=submission_id))
        self.assertEqual([], self.accepted_events())

    def test_unknown_fields_are_refused(self):
        submission_id = self.awaiting_review()
        self.error("INVALID_INPUT", "review_dispatched",
                   self.scope(submission_id=submission_id, reviewer="认错字了"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
