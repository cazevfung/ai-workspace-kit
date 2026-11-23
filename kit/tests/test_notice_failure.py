"""AW-114 C2：目标会话归档时，执行会话有一条落 failed 的落库路径。

现场是这类：调度建的协调会话在自己那一轮结束时被宿主归档（本机实测 `status=archived`
与 `last_turn_ended_at` 相差 22–26 ms），执行会话做完时 `send_to_session` 回 `ARCHIVED`，
而 `record_notice` 只许协调会话调——于是「回执没送到」这件事在库里根本没有位置，
`merge_ticket` 又按 `NO_NOTICE` 拒合。

`report_notice_failure` 是这条路的落点：执行会话记**自己**那次投递失败，记在它自己那张单上。

这一批用例判五件事：

1. 执行会话能记一条 `failed`，之后 `merge_ticket` 过得去（`failed` 也算走过回执）。
2. 执行会话**造不出** `sent`：操作里没有 `status` 字段，带它就报 `INVALID_INPUT`；
   `one_sent_notice` 这条唯一索引因此不可能被它碰到。
3. 只有这张尝试绑定的执行会话能记：协调会话、路人会话都报 `SESSION_MISMATCH`（与 `submit_result`
   同一个判据，不另造一个码）。
4. `target_session_id` 必须就是该尝试的协调会话，写错报 `NOT_TARGET`，一行不写。
5. 记完之后 `events` 里有一条行，带上记录人的会话 id——「谁记的」事后查得到。
"""
import tempfile
import unittest
from pathlib import Path

from task_center import Center

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "89abcdef0123456789abcdef0123456789abcdef"


class NoticeFailureTest(unittest.TestCase):
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
        self.ok("register_project", {"project_id": "p1", "name": "项目一", "root": str(self.project)})

    # --- 夹具 -------------------------------------------------------------

    def call(self, op, payload=None, request_id=None):
        self.counter += 1
        return self.center.execute(op, self.resolve(payload or {}), request_id or f"req-{self.counter}")

    def resolve(self, payload):
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

    def ticket(self):
        created = self.ok("create_ticket", {
            "project_id": "p1", "title": "整理检查清单",
            "deliverable": "一份逐项可勾选的检查清单",
            "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        })
        self.ids["t1"] = created["id"]
        return created

    def done(self, coordinator="coordinator-1", executor="executor-1"):
        self.ticket()
        claim = self.ok("claim_ticket", self.scope(expected_revision=1, coordinator_session_id=coordinator))
        self.ok("bind_session", self.scope(attempt_id=claim["attempt_id"], session_id=executor,
                                          project_root=str(self.project)))
        (self.project / "result.md").write_text("1. 检查步骤", encoding="utf-8")
        submission = self.ok("submit_result", {
            **self.scope(), "attempt_id": claim["attempt_id"], "session_id": executor,
            "artifacts": ["result.md"], "summary": "清单已完成",
            "evidence": ["人工检查条目"], "risks": [], "head_sha": HEAD_SHA, "base_sha": BASE_SHA,
        })
        self.ok("review_result", {
            **self.scope(), "attempt_id": claim["attempt_id"],
            "submission_id": submission["submission_id"], "reviewer_session_id": "reviewer-1",
            "verdict": "accept",
            "checks": [{"id": "C1", "passed": True, "evidence": "已阅读清单并核对步骤"}],
            "reason": "逐项检查后作出结论",
        })
        return claim["attempt_id"]

    def failure_payload(self, attempt, session="executor-1", **extra):
        return {**self.scope(), "attempt_id": attempt, "session_id": session,
                "target_session_id": "coordinator-1", "wake_kind": "unknown",
                "detail": "send_to_session 返回 ARCHIVED：目标会话 coordinator-1 已被宿主归档，"
                          "回执没有送达", **extra}

    def merge_payload(self, **extra):
        return {**self.scope(), "commit": "a1b2c3d", "target_branch": "master",
                "tests": "python tools/run_tests.py：全绿", "reviewed_sha": HEAD_SHA, **extra}

    def notices(self):
        return self.ok("get_ticket", self.scope())["notices"]

    # --- 落点 -------------------------------------------------------------

    def test_the_executing_session_can_record_its_own_failed_receipt(self):
        attempt = self.done()
        receipt = self.ok("report_notice_failure", self.failure_payload(attempt))
        self.assertEqual("failed", receipt["status"])
        self.assertEqual("NOTIFY_FAILED", receipt["alert"])
        self.assertEqual("executor-1", receipt["reported_by_session_id"])
        self.assertEqual("coordinator-1", receipt["target_session_id"])
        rows = self.notices()
        self.assertEqual(1, len(rows))
        self.assertEqual("failed", rows[0]["status"])
        self.assertEqual("executor-1", rows[0]["origin_session_id"])
        self.assertIn("ARCHIVED", rows[0]["detail"])
        # 一条 failed 也算走过回执：合并这道门放行，而 sent 的唯一性没有被碰过。
        self.ok("merge_ticket", self.merge_payload())
        self.assertEqual([], self.ok("list_unmerged")["items"])

    def test_the_executing_session_cannot_declare_delivery(self):
        attempt = self.done()
        for bad in ("sent", "SENT", "failed"):
            self.error("INVALID_INPUT", "report_notice_failure",
                       self.failure_payload(attempt, status=bad))
        self.assertEqual([], self.notices())

    def test_only_the_bound_executing_session_may_report_a_failure(self):
        attempt = self.done()
        for stranger in ("coordinator-1", "coordinator-2", "someone-else"):
            self.error("SESSION_MISMATCH", "report_notice_failure",
                       self.failure_payload(attempt, session=stranger))
        self.assertEqual([], self.notices())

    def test_target_must_be_the_coordinator_of_that_attempt(self):
        attempt = self.done()
        for target in ("executor-1", "coordinator-2", "origin-session-1"):
            self.error("NOT_TARGET", "report_notice_failure",
                       self.failure_payload(attempt, target_session_id=target))
        self.assertEqual([], self.notices())

    def test_the_report_is_a_queryable_event_naming_who_recorded_it(self):
        attempt = self.done()
        self.ok("report_notice_failure", self.failure_payload(attempt))
        rows = [row for row in self.ok("events", self.scope(limit=100))["items"]
                if row["operation"] == "report_notice_failure"]
        self.assertEqual(1, len(rows))
        self.assertEqual("executor-1", rows[0]["detail"]["result"]["reported_by_session_id"])
        self.assertEqual("failed", rows[0]["detail"]["result"]["status"])

    def test_a_failure_does_not_touch_state_or_the_sent_row(self):
        attempt = self.done()
        before = self.ok("get_ticket", self.scope())
        self.ok("report_notice_failure", self.failure_payload(attempt))
        after = self.ok("get_ticket", self.scope())
        self.assertEqual(before["state"], after["state"])
        self.assertEqual(before["revision"], after["revision"])
        self.assertEqual("done", after["state"])
        # 协调会话后来把回执补记成 sent 也不冲突：failed 不占 sent 那一条。
        self.ok("record_notice", {**self.scope(), "attempt_id": attempt, "session_id": "coordinator-1",
                                  "origin_session_id": "executor-1", "status": "sent",
                                  "target_session_id": "coordinator-1", "wake_kind": "unknown",
                                  "detail": "接替会话代为确认：回执已在这一次会话里送达"})
        self.assertEqual(["failed", "sent"], [row["status"] for row in self.notices()])

    def test_fields_are_checked_before_anything_is_written(self):
        attempt = self.done()
        payload = self.failure_payload(attempt)
        for key in ("attempt_id", "session_id", "target_session_id", "detail"):
            self.error("INVALID_INPUT", "report_notice_failure",
                       {k: v for k, v in payload.items() if k != key})
        for bad in ("", "  ", 7, None, "x" * 41, "already active"):
            self.error("INVALID_INPUT", "report_notice_failure",
                       self.failure_payload(attempt, wake_kind=bad))
        self.error("STALE_ATTEMPT", "report_notice_failure", self.failure_payload("nope"))
        self.error("PROJECT_MISMATCH", "report_notice_failure",
                   {**self.failure_payload(attempt), "project_id": "p2"})
        self.assertEqual([], self.notices())


if __name__ == "__main__":
    unittest.main()
