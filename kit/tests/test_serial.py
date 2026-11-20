"""schema 8：派发串行——同项目一次只跑一张单，要并行必须显式开口（AW-061）。

依据 `tasks/plan-012-回执闭环与合并断点.md` 的任务 C。这里判五件事：

1. 同项目已有在途工单时，第二张单报 `DISPATCH_BUSY`，消息里点名是哪张单、停在哪一步；
   四种在途状态（dispatching / running / awaiting_review / blocked）逐个验一遍。
2. 显式 `allow_parallel: true` 才越过；越过要留一条事件，并在回执里原样回显越过了谁。
3. `done` 与 `cancelled` 不算在途，不挡下一张单。
4. 另一个项目的在途单不挡本项目：串行的范围是项目，不是整台机器。
5. `rework` 不是免排队通道——走过一轮返工的单复工时照样排在在途单后面。

为什么默认挡：计划里那张单的起因就是前 55 张单里有 76 对时间窗重叠（AW-046 与 AW-047
相隔 2 秒领取）。库不会自己知道「现在算不算在并行」，所以把「一次一张」做成默认路径，
要并行就得写下一行 `allow_parallel`，事后在事件表里看得见这是故意的。
"""
import json
import tempfile
import unittest
from pathlib import Path

from task_center import Center


class SerialDispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.projects = {}
        for pid, dirname in (("p1", "项目一"), ("p2", "项目二")):
            project = self.root / dirname
            project.mkdir()
            (project / "PRD.md").write_text("需求第一版", encoding="utf-8")
            self.projects[pid] = project
        self.db = self.root / "center.sqlite3"
        self.center = Center(self.db)
        self.counter = 0
        self.ok("init")
        for pid, name in (("p1", "项目一"), ("p2", "项目二")):
            self.ok("register_project", {"project_id": pid, "name": name, "root": str(self.projects[pid])})

    # --- 夹具 -------------------------------------------------------------

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

    def scope(self, ticket_id="t1", project_id="p1", **extra):
        return {"project_id": project_id, "ticket_id": ticket_id, **extra}

    def ticket(self, ticket_id="t1", project_id="p1"):
        return self.ok("create_ticket", {
            "ticket_id": ticket_id, "project_id": project_id, "title": "整理检查清单",
            "deliverable": "一份逐项可勾选的检查清单",
            "prd_path": "PRD.md", "criteria": [{"id": "C1", "text": "包含明确的检查步骤"}],
        })

    def claim(self, ticket_id="t1", project_id="p1", revision=1, coordinator="coordinator-1", **extra):
        return self.ok("claim_ticket", self.scope(
            ticket_id=ticket_id, project_id=project_id, expected_revision=revision,
            coordinator_session_id=coordinator, **extra))

    def bind(self, attempt, session="executor-1", ticket_id="t1", project_id="p1"):
        return self.ok("bind_session", self.scope(
            ticket_id=ticket_id, project_id=project_id, attempt_id=attempt,
            session_id=session, project_root=str(self.projects[project_id])))

    def advance(self, state, ticket_id="t1", project_id="p1", session="executor-1"):
        """把一张单推到四种在途状态之一，返回它占住的尝试编号。"""
        attempt = self.claim(ticket_id=ticket_id, project_id=project_id,
                             coordinator=f"coordinator-{ticket_id}")["attempt_id"]
        if state == "dispatching":
            return attempt
        self.bind(attempt, session=session, ticket_id=ticket_id, project_id=project_id)
        if state == "running":
            return attempt
        if state == "blocked":
            self.ok("ask_ticket", self.scope(
                ticket_id=ticket_id, project_id=project_id, attempt_id=attempt, session_id=session,
                question="这批差异算缺陷还是口径问题？"))
            return attempt
        self.assertEqual("awaiting_review", state)
        result = self.projects[project_id] / f"result-{ticket_id}.md"
        result.write_text("1. 检查步骤", encoding="utf-8")
        self.ok("submit_result", self.scope(
            ticket_id=ticket_id, project_id=project_id, attempt_id=attempt, session_id=session,
            artifacts=[result.name], summary="清单已完成", evidence=["人工检查条目"], risks=[],
            head_sha="0123456789abcdef0123456789abcdef01234567"))
        return attempt

    def events(self, ticket_id="t1", project_id="p1"):
        return self.ok("events", self.scope(ticket_id=ticket_id, project_id=project_id))["items"]

    # --- 默认挡 ------------------------------------------------------------

    def test_second_ticket_in_same_project_is_rejected_and_named(self):
        self.ticket("t1")
        self.ticket("t2")
        self.advance("dispatching", "t1")
        rejected = self.error("DISPATCH_BUSY", "claim_ticket", self.scope(
            ticket_id="t2", expected_revision=1, coordinator_session_id="coordinator-2"))
        message = rejected["error"]["message"]
        # 拒绝要能直接回答「谁占着」：光说 busy，协调会话还得自己去翻。
        self.assertIn("t1", message)
        self.assertIn("dispatching", message)
        self.assertIn("allow_parallel", message)
        second = self.ok("get_ticket", self.scope("t2"))
        self.assertEqual("ready", second["state"])
        self.assertEqual([], second["attempts"])

    def test_every_in_flight_state_blocks_the_next_ticket(self):
        for state in ("dispatching", "running", "awaiting_review", "blocked"):
            with self.subTest(state=state):
                # 每个子用例一张新库：一张单停在 awaiting_review 就再也回收不了。
                self.setUp()
                self.ticket("t1")
                self.ticket("t2")
                self.advance(state, "t1")
                rejected = self.error("DISPATCH_BUSY", "claim_ticket", self.scope(
                    ticket_id="t2", expected_revision=1, coordinator_session_id="coordinator-2"))
                self.assertIn(f"t1 ({state})", rejected["error"]["message"])

    # --- 显式越过 ----------------------------------------------------------

    def test_allow_parallel_lets_the_second_ticket_through_and_leaves_a_trace(self):
        self.ticket("t1")
        self.ticket("t2")
        self.advance("running", "t1")
        receipt = self.claim(ticket_id="t2", coordinator="coordinator-2", allow_parallel=True)
        self.assertTrue(receipt["allow_parallel"])
        self.assertEqual([{"ticket_id": "t1", "state": "running"}], receipt["parallel_with"])
        self.assertEqual("dispatching", receipt["state"])
        # 越过要留一行事件：事后看事件表就知道这次并行是故意的，不是漏了检查。
        overrides = [item for item in self.events("t2")
                     if item["detail"].get("allow_parallel_override") is True]
        self.assertEqual(1, len(overrides))
        self.assertEqual([{"ticket_id": "t1", "state": "running"}], overrides[0]["detail"]["parallel_with"])
        # 没越过时回执也说得清楚：allow_parallel 与 parallel_with 都在，值是空的。
        first = [item for item in self.events("t1") if item["operation"] == "claim_ticket"][0]
        self.assertEqual([], first["detail"]["result"]["parallel_with"])
        self.assertFalse(first["detail"]["result"]["allow_parallel"])

    def test_allow_parallel_is_recorded_as_given_boolean_only(self):
        self.ticket("t1")
        self.advance("running", "t1")
        self.ticket("t2")
        for bad in (1, 0, "true", "yes", None, []):
            with self.subTest(allow_parallel=bad):
                self.error("INVALID_INPUT", "claim_ticket", self.scope(
                    ticket_id="t2", expected_revision=1, coordinator_session_id="coordinator-2",
                    allow_parallel=bad))
        self.assertEqual([], self.ok("get_ticket", self.scope("t2"))["attempts"])
        # false 与不传等价：都不是越过，仍然被挡。
        self.error("DISPATCH_BUSY", "claim_ticket", self.scope(
            ticket_id="t2", expected_revision=1, coordinator_session_id="coordinator-2",
            allow_parallel=False))

    # --- 什么不算在途 ------------------------------------------------------

    def test_done_ticket_does_not_block_the_next_one(self):
        self.ticket("t1")
        attempt = self.advance("awaiting_review", "t1")
        submission = self.ok("get_ticket", self.scope("t1"))["submissions"][0]["id"]
        self.ok("review_result", self.scope(
            attempt_id=attempt, submission_id=submission, reviewer_session_id="reviewer-1",
            verdict="accept", checks=[{"id": "C1", "passed": True, "evidence": "逐项核对"}],
            reason="验收通过"))
        self.assertEqual("done", self.ok("get_ticket", self.scope("t1"))["state"])
        self.ticket("t2")
        receipt = self.claim(ticket_id="t2", coordinator="coordinator-2")
        self.assertEqual([], receipt["parallel_with"])

    def test_cancelled_ticket_does_not_block_the_next_one(self):
        self.ticket("t1")
        self.advance("running", "t1")
        self.ok("cancel_ticket", self.scope(expected_revision=1, reason="用户取消"))
        self.ticket("t2")
        receipt = self.claim(ticket_id="t2", coordinator="coordinator-2")
        self.assertEqual([], receipt["parallel_with"])

    def test_in_flight_ticket_in_another_project_does_not_block(self):
        self.ticket("t1", project_id="p1")
        self.ticket("q1", project_id="p2")
        self.advance("running", "t1", project_id="p1")
        # 串行的范围是项目：两个项目各自推进，不该互相排队。
        receipt = self.claim(ticket_id="q1", project_id="p2", coordinator="coordinator-p2")
        self.assertEqual([], receipt["parallel_with"])
        self.assertEqual("dispatching", self.ok("get_ticket", self.scope("q1", "p2"))["state"])

    def test_rework_ticket_still_queues_behind_an_in_flight_ticket(self):
        self.ticket("t1")
        attempt = self.advance("awaiting_review", "t1")
        submission = self.ok("get_ticket", self.scope("t1"))["submissions"][0]["id"]
        self.ok("review_result", self.scope(
            attempt_id=attempt, submission_id=submission, reviewer_session_id="reviewer-1",
            verdict="reject", checks=[{"id": "C1", "passed": False, "evidence": "缺回滚步骤"}],
            reason="补回滚步骤"))
        self.assertEqual("rework", self.ok("get_ticket", self.scope("t1"))["state"])
        # t1 回到 rework 之后自己不再占着项目，所以 t2 领得走。
        self.ticket("t2")
        self.claim(ticket_id="t2", revision=1, coordinator="coordinator-2")
        # 但 t1 复工时排在在途的 t2 后面：走过一轮返工不是免排队通道。
        # 拒收不改 revision（rework 原地等待），所以依然是 1。
        rejected = self.error("DISPATCH_BUSY", "claim_ticket", self.scope(
            ticket_id="t1", expected_revision=1, coordinator_session_id="coordinator-1"))
        self.assertIn("t2 (dispatching)", rejected["error"]["message"])
        # 显式开口才能同时推进两张单。
        resumed = self.claim(ticket_id="t1", revision=1, coordinator="coordinator-1", allow_parallel=True)
        self.assertEqual([{"ticket_id": "t2", "state": "dispatching"}], resumed["parallel_with"])


if __name__ == "__main__":
    unittest.main()
