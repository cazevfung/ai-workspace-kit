"""AW-114 C2–C7：把「授权」与「审计」拆成两个字段——owner 可变，origin 不可变。

要解的死锁现场是 AW-106：`state=done`、review `accept`、`notices` 0 行、`merges` 0 行。
`record_notice` 以前要求 `session_id` 等于该尝试的 `coordinator_session_id`，而那条会话
已经在宿主里 `status=archived`，没人能补这条回执；`merge_ticket` 又按 `NO_NOTICE` 拒合。
成果在库里，进不了 master。

一个字段担两种语义就是那个死锁的根：

- `attempts.coordinator_session_id` 是 **origin**——谁派的这条尝试，历史事实，永不改写；
- `tickets.owner_session_id` 是 **owner**——现在谁负责收尾，人换了就换，`take_over_ticket` 改它。

授权（`record_notice`）读 owner，审计（回执发给了谁、谁派的单）读 origin。这一批用例判：

1. **未接替时仍然拒**：非 owner 记回执报 `NOT_COORDINATOR`，`merge_ticket` 报 `NO_NOTICE`。
2. **接替之后路是通的**：`take_over_ticket` 换掉 owner（C3 的 payload 只有四个字段），
   之后新 owner 记 `sent`、合并照原路走；origin 一个字都没改。
3. **接替本身是可查的事实**：一条 `events` 行，新旧 owner 与理由都在。
4. **旧 owner 交出授权**：接替之后再记回执报 `NOT_COORDINATOR`。
5. 拒绝路径：`ALREADY_OWNER`、`TAKEOVER_BY_EXECUTOR`、`INVALID_STATE`（已取消）。

接替是一次自报声明（`new_owner_session_id` 仍是字符串），工具不认证它，也验证不了宿主里
那条会话是否真的死了。用例只判「说出来的是不是完整的、后续动作是不是按它走」。
"""
import tempfile
import unittest
from pathlib import Path

from task_center import Center

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "89abcdef0123456789abcdef0123456789abcdef"


class OwnerTest(unittest.TestCase):
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

    def claim(self, coordinator="coordinator-1"):
        return self.ok("claim_ticket", self.scope(expected_revision=1, coordinator_session_id=coordinator))

    def done(self, coordinator="coordinator-1", executor="executor-1"):
        self.ticket()
        claim = self.claim(coordinator)
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

    def take_over_payload(self, **extra):
        """C3 的 payload 就这四个字段：不要求 attempt_id，也不要求 expected_revision。"""
        return {**self.scope(), "new_owner_session_id": "coordinator-2",
                "reason": "原协调会话 404129f5 已归档（宿主 status=archived），由本轮协调会话接替", **extra}

    def notice_payload(self, attempt, session="coordinator-2", status="sent", **extra):
        return {**self.scope(), "attempt_id": attempt, "session_id": session,
                "origin_session_id": "executor-1", "status": status,
                "target_session_id": session, "wake_kind": "queued",
                "detail": "已把回执发回接替后的负责人", **extra}

    def merge_payload(self, **extra):
        return {**self.scope(), "commit": "a1b2c3d", "target_branch": "master",
                "tests": "python tools/run_tests.py：全绿", "reviewed_sha": HEAD_SHA, **extra}

    def events(self, operation=None):
        rows = self.ok("events", self.scope(limit=100))["items"]
        return [row for row in rows if operation is None or row["operation"] == operation]

    # --- owner 从哪来 -----------------------------------------------------

    def test_claim_makes_the_dispatching_session_the_owner(self):
        self.ticket()
        self.assertIsNone(self.ok("get_ticket", self.scope())["owner_session_id"])
        self.claim()
        ticket = self.ok("get_ticket", self.scope())
        self.assertEqual("coordinator-1", ticket["owner_session_id"])
        self.assertEqual("coordinator-1", ticket["attempts"][-1]["coordinator_session_id"])

    # --- 未接替时：门照旧关着（C4） ---------------------------------------

    def test_without_takeover_a_new_session_still_cannot_record_a_notice(self):
        attempt = self.done()
        for stranger in ("coordinator-2", "someone-else"):
            self.error("NOT_COORDINATOR", "record_notice", self.notice_payload(attempt, session=stranger))
        self.error("NO_NOTICE", "merge_ticket", self.merge_payload())
        ticket = self.ok("get_ticket", self.scope())
        self.assertEqual("done", ticket["state"])
        self.assertEqual([], ticket["notices"])
        self.assertEqual([], ticket["merges"])
        self.assertEqual("coordinator-1", ticket["owner_session_id"])

    # --- 接替之后：回执与合并都走原路（C3 / C6 的机制部分） ---------------

    def test_after_takeover_the_receipt_and_the_merge_go_through(self):
        attempt = self.done()
        receipt = self.ok("take_over_ticket", self.take_over_payload())
        self.assertEqual("coordinator-1", receipt["previous_owner_session_id"])
        self.assertEqual("coordinator-2", receipt["owner_session_id"])
        self.assertEqual("coordinator-1", receipt["origin_session_id"])
        self.assertEqual("done", receipt["state"])
        self.assertEqual(1, receipt["revision"])
        ticket = self.ok("get_ticket", self.scope())
        self.assertEqual("coordinator-2", ticket["owner_session_id"])
        # origin 不动：谁派的这条尝试仍是历史事实，接替不改它。
        self.assertEqual("coordinator-1", ticket["attempts"][-1]["coordinator_session_id"])
        self.assertEqual(attempt, ticket["current_attempt"])
        notice = self.ok("record_notice", self.notice_payload(attempt))
        self.assertEqual("sent", notice["status"])
        merge = self.ok("merge_ticket", self.merge_payload())
        self.assertEqual("a1b2c3d", merge["commit"])
        # 合上了就从「已验收但没合并」的清单里消失：死单真的解开了。
        self.assertEqual([], self.ok("list_unmerged")["items"])

    def test_take_over_leaves_a_queryable_event_with_both_owners(self):
        self.done()
        self.ok("take_over_ticket", self.take_over_payload())
        rows = self.events("take_over_ticket")
        self.assertEqual(1, len(rows))
        detail = rows[0]["detail"]["result"]
        self.assertEqual("coordinator-1", detail["previous_owner_session_id"])
        self.assertEqual("coordinator-2", detail["owner_session_id"])
        self.assertIn("404129f5", detail["reason"])
        self.assertTrue(rows[0]["created_at"])

    def test_the_old_owner_loses_the_authority_to_record(self):
        attempt = self.done()
        self.ok("take_over_ticket", self.take_over_payload())
        self.error("NOT_COORDINATOR", "record_notice",
                   self.notice_payload(attempt, session="coordinator-1", target_session_id="coordinator-1"))
        # 新 owner 仍然可以记 failed：授权换人，事实不变。
        self.ok("record_notice", self.notice_payload(attempt, status="failed",
                                                    detail="回执没有送达原协调会话，由接替者补记"))
        self.assertEqual(["failed"], [row["status"] for row in self.ok("get_ticket", self.scope())["notices"]])

    # --- 拒绝路径：一行不写 -----------------------------------------------

    def test_take_over_refuses_the_current_owner(self):
        self.done()
        self.error("ALREADY_OWNER", "take_over_ticket",
                   self.take_over_payload(new_owner_session_id="coordinator-1"))
        self.assertEqual("coordinator-1", self.ok("get_ticket", self.scope())["owner_session_id"])

    def test_take_over_refuses_the_executing_session(self):
        self.done()
        self.error("TAKEOVER_BY_EXECUTOR", "take_over_ticket",
                   self.take_over_payload(new_owner_session_id="executor-1"))
        self.assertEqual("coordinator-1", self.ok("get_ticket", self.scope())["owner_session_id"])

    def test_take_over_refuses_a_cancelled_ticket(self):
        self.ticket()
        claim = self.claim()
        self.ok("cancel_ticket", self.scope(expected_revision=1, reason="不做了"))
        self.assertEqual("cancelled", self.ok("get_ticket", self.scope())["state"])
        self.error("INVALID_STATE", "take_over_ticket", self.take_over_payload())
        # 取消不清空 owner：它记的是「谁当时负责」，不是「现在还能不能接手」。
        self.assertEqual("coordinator-1", self.ok("get_ticket", self.scope())["owner_session_id"])
        self.assertTrue(claim["attempt_id"])

    def test_take_over_checks_its_fields(self):
        self.done()
        payload = self.take_over_payload()
        for key in ("new_owner_session_id", "reason"):
            self.error("INVALID_INPUT", "take_over_ticket", {k: v for k, v in payload.items() if k != key})
        for bad in ("", "  ", 7, None):
            self.error("INVALID_INPUT", "take_over_ticket", self.take_over_payload(new_owner_session_id=bad))
        self.error("INVALID_INPUT", "take_over_ticket", {**payload, "note": "多余字段"})
        self.error("NOT_FOUND", "take_over_ticket", {**payload, "ticket_id": "absent"})
        self.error("PROJECT_MISMATCH", "take_over_ticket", {**payload, "project_id": "p2"})
        self.assertEqual("coordinator-1", self.ok("get_ticket", self.scope())["owner_session_id"])


if __name__ == "__main__":
    unittest.main()
