"""schema 6：决策通道——工单会「在等一句话」，而这句话有人答、有记录、能被查。

依据 `PRD/PRD-005-决策通道.md` v0.3 的 4.1–4.7。这里判的都是「库里说得清」：

1. `blocked` 两处都写（`tickets.state` 与 `attempts.state`），权威在工单级；`revision` 不变。
2. `blocked` 期间 `submit_result` 必须被拒（`INVALID_STATE`）——成果只能在答案回来后交。
3. 答案落库后工单回 `running`，同一尝试接着 `submit_result` 能过。
4. 被 `recover_ticket` 作废的尝试，它的问题转 `voided`；晚到的答案报 `QUESTION_MISMATCH`。
5. `blocked` 占用活跃尝试：同一会话在等答案期间绑不上第二张单（`CONFLICT`）。

两条「兜底码」在正常路径上撞不到，这一点写在这里以免被当成漏测：`ALREADY_ASKED` 与
`answer_ticket` 的 `STALE_ATTEMPT` 都被「工单状态」与「问题是否 open」挡在前面
（`blocked` 工单再问一句是 `INVALID_STATE`，作废的问题再答是 `QUESTION_MISMATCH`）。
它们防的是库里出现「open 问题配 running 工单」这类不一致行，所以要拿 SQL 直接造出来才测得到——
`one_open_question` 索引与状态同步写让这种行在生产路径上写不进去。
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from task_center import Center


class DecisionChannelTest(unittest.TestCase):
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

    def error(self, code, op, payload=None, request_id=None):
        result = self.call(op, payload, request_id)
        self.assertFalse(result["ok"], result)
        self.assertEqual(code, result["error"]["code"], result)
        return result

    def scope(self, **extra):
        return {"project_id": "p1", "ticket_id": "t1", **extra}

    def ticket(self, ticket_id="t1"):
        return self.ok("create_ticket", {
            "ticket_id": ticket_id, "project_id": "p1", "title": "整理检查清单",
            "deliverable": "一份逐项可勾选的检查清单",
            "prd_path": "PRD.md", "criteria": [{"id": "C1", "text": "包含明确的检查步骤"}],
        })

    def running(self, session="executor-1", ticket_id="t1", coordinator="coordinator-1", revision=1,
                allow_parallel=False):
        scope = self.scope(ticket_id=ticket_id, expected_revision=revision, coordinator_session_id=coordinator)
        if allow_parallel:
            scope["allow_parallel"] = True
        claim = self.ok("claim_ticket", scope)
        self.ok("bind_session", self.scope(ticket_id=ticket_id, attempt_id=claim["attempt_id"],
                                           session_id=session, project_root=str(self.project)))
        return claim["attempt_id"]

    def ask_payload(self, attempt, session="executor-1", ticket_id="t1", **extra):
        return {"project_id": "p1", "ticket_id": ticket_id, "attempt_id": attempt, "session_id": session,
                "question": "这批差异算缺陷还是口径问题？",
                "options": [{"id": "A", "text": "算缺陷，按缺陷修"},
                            {"id": "B", "text": "算口径，改文档"}], **extra}

    def answer_payload(self, question_id, **extra):
        return self.scope(question_id=question_id, answer="算口径问题，改文档，不改代码",
                          answered_by="human", prd_sha256=self.prd_sha256(), **extra)

    def prd_sha256(self, ticket_id="t1"):
        return self.ok("get_ticket", self.scope(ticket_id=ticket_id))["prd_sha256"]

    def blocked(self):
        attempt = self.running()
        return attempt, self.ok("ask_ticket", self.ask_payload(attempt))["question_id"]

    def result_payload(self, attempt, session="executor-1"):
        (self.project / "result.md").write_text("1. 检查步骤", encoding="utf-8")
        return self.scope(attempt_id=attempt, session_id=session, artifacts=["result.md"],
                          summary="清单已完成", evidence=["人工检查条目"], risks=[])

    def sql(self, *statements):
        """直接改库，只用于造出不一致行来测兜底码；正常路径靠 API 走不到这些状态。"""
        conn = sqlite3.connect(self.db)
        try:
            for statement in statements:
                conn.execute(statement)
            conn.commit()
        finally:
            conn.close()

    # --- 提问：两处 blocked、revision 不变 ---------------------------------

    def test_ask_blocks_ticket_and_attempt_without_bumping_revision(self):
        self.ticket()
        attempt = self.running()
        receipt = self.ok("ask_ticket", self.ask_payload(attempt))
        self.assertEqual("blocked", receipt["state"])
        self.assertEqual("blocked", receipt["attempt_state"])
        self.assertEqual(1, receipt["revision"])
        self.assertTrue(receipt["question_id"].startswith("q-"))
        ticket = self.ok("get_ticket", self.scope())
        self.assertEqual("blocked", ticket["state"])
        self.assertEqual(1, ticket["revision"])
        self.assertEqual("blocked", ticket["attempts"][0]["state"])
        self.assertEqual(1, len(ticket["questions"]))
        question = ticket["questions"][0]
        self.assertEqual(receipt["question_id"], question["id"])
        self.assertEqual("open", question["state"])
        self.assertEqual(attempt, question["attempt_id"])
        self.assertEqual(["A", "B"], [item["id"] for item in question["options"]])
        self.assertEqual([], ticket["answers"])
        # 状态变化留事件：协调会话不必去问执行会话「你在等什么」。
        operations = [item["operation"] for item in self.ok("events", self.scope())["items"]]
        self.assertIn("ask_ticket", operations)
        self.assertEqual(receipt["asked_at"], question["asked_at"])

    def test_open_question_can_be_typed_without_options(self):
        self.ticket()
        attempt = self.running()
        payload = self.ask_payload(attempt)
        payload.pop("options")
        receipt = self.ok("ask_ticket", payload)
        question = self.ok("get_ticket", self.scope(ticket_id="t1"))["questions"][0]
        self.assertEqual([], question["options"])
        self.assertEqual(receipt["question_id"], question["id"])
        # 空数组与不传等价：开放题。第二张单与本用例无关，显式越过串行（AW-061）。
        self.ticket("t2")
        second = self.running(ticket_id="t2", session="executor-2", allow_parallel=True)
        self.assertTrue(self.call("ask_ticket", self.ask_payload(second, session="executor-2",
                                                               ticket_id="t2", options=[]))["ok"])

    # --- 等待期间不能交成果 ------------------------------------------------

    def test_blocked_ticket_rejects_submit_result(self):
        self.ticket()
        attempt, _ = self.blocked()
        result = self.error("INVALID_STATE", "submit_result", self.result_payload(attempt))
        self.assertEqual("Ticket is not accepting execution results", result["error"]["message"])
        self.assertEqual("blocked", self.ok("get_ticket", self.scope())["state"])

    def test_blocked_ticket_rejects_second_question(self):
        self.ticket()
        attempt, question = self.blocked()
        # 再问一句是状态错，不是「问题已存在」：两条一起说清「该去答，不是该再问」。
        self.error("INVALID_STATE", "ask_ticket", self.ask_payload(attempt))
        self.assertEqual("open", self.ok("get_ticket", self.scope())["questions"][0]["state"])
        self.assertEqual(question, self.ok("get_ticket", self.scope())["questions"][0]["id"])

    # --- 答案落库：回 running、同一尝试能接着交 -----------------------------

    def test_answer_returns_ticket_to_running_and_same_attempt_submits(self):
        self.ticket()
        attempt, question = self.blocked()
        receipt = self.ok("answer_ticket", self.answer_payload(question))
        self.assertEqual("running", receipt["ticket_state"])
        self.assertEqual("answered", receipt["question_state"])
        self.assertEqual(1, receipt["revision"])
        ticket = self.ok("get_ticket", self.scope())
        self.assertEqual("running", ticket["state"])
        self.assertEqual("running", ticket["attempts"][0]["state"])
        self.assertEqual("answered", ticket["questions"][0]["state"])
        self.assertEqual(1, len(ticket["answers"]))
        answer = ticket["answers"][0]
        self.assertEqual(receipt["answer_id"], answer["id"])
        self.assertEqual(question, answer["question_id"])
        self.assertEqual("算口径问题，改文档，不改代码", answer["answer"])
        self.assertEqual("human", answer["answered_by"])
        self.assertIsNone(answer["answered_by_session_id"])
        # 同一尝试继续交，不需要重新领取。
        submitted = self.ok("submit_result", self.result_payload(attempt))
        self.assertEqual("awaiting_review", submitted["state"])
        self.assertEqual(attempt, self.ok("get_ticket", self.scope())["current_attempt"])

    def test_one_question_accepts_exactly_one_answer(self):
        self.ticket()
        _, question = self.blocked()
        self.ok("answer_ticket", self.answer_payload(question))
        self.error("QUESTION_MISMATCH", "answer_ticket", self.answer_payload(question))

    # --- 作废与迟到答案 ----------------------------------------------------

    def test_recover_voids_question_and_late_answer_is_question_mismatch(self):
        self.ticket()
        attempt, question = self.blocked()
        recovered = self.ok("recover_ticket", self.scope(attempt_id=attempt, expected_revision=1,
                                                        reason="等太久，先收回"))
        self.assertEqual("ready", recovered["state"])
        self.assertEqual([question], recovered["voided_question_ids"])
        self.assertEqual(2, recovered["revision"])
        ticket = self.ok("get_ticket", self.scope())
        self.assertEqual("voided", ticket["questions"][0]["state"])
        self.assertEqual("superseded", ticket["attempts"][0]["state"])
        # 问题已作废，此刻才到的答案不能再写进已失效的尝试。
        self.error("QUESTION_MISMATCH", "answer_ticket", self.answer_payload(question))
        self.assertEqual([], self.ok("get_ticket", self.scope())["answers"])

    def test_old_attempt_cannot_ask_again_after_recover(self):
        self.ticket()
        attempt, _ = self.blocked()
        self.ok("recover_ticket", self.scope(attempt_id=attempt, expected_revision=1, reason="收回"))
        # 旧尝试的编号不能再提问：它已被 supersede，工单 revision 也变了。
        self.error("STALE_ATTEMPT", "ask_ticket", self.ask_payload(attempt))
        # 新尝试要重新提出新问题，拿新的 question_id。
        fresh = self.running(revision=2)
        self.assertNotEqual(attempt, fresh)
        receipt = self.ok("ask_ticket", self.ask_payload(fresh))
        self.assertEqual("blocked", receipt["state"])
        self.assertEqual(2, len(self.ok("get_ticket", self.scope())["questions"]))

    def test_answer_on_question_left_open_on_superseded_attempt_is_stale(self):
        # 兜底路径：直接把「open 问题 + 已失效尝试」造进库。
        self.ticket()
        attempt, question = self.blocked()
        self.ok("recover_ticket", self.scope(attempt_id=attempt, expected_revision=1, reason="收回"))
        self.sql(f"UPDATE questions SET state='open' WHERE id='{question}'",
                 "UPDATE tickets SET state='blocked' WHERE id='t1'")
        result = self.error("STALE_ATTEMPT", "answer_ticket", self.answer_payload(question))
        self.assertIn("no longer current", result["error"]["message"])
        # 造出来的不一致行不会因为一次被拒的调用就被改写。
        self.assertEqual("blocked", self.ok("get_ticket", self.scope())["state"])

    def test_second_question_on_inconsistent_row_reports_already_asked(self):
        # 兜底路径：工单被改回 running，而那条问题仍是 open。索引与状态同步写让它在正常路径上写不进去。
        self.ticket()
        attempt, question = self.blocked()
        self.sql("UPDATE tickets SET state='running' WHERE id='t1'",
                 f"UPDATE attempts SET state='running' WHERE id='{attempt}'")
        self.error("ALREADY_ASKED", "ask_ticket", self.ask_payload(attempt))
        self.assertEqual(1, len(self.ok("get_ticket", self.scope())["questions"]))

    # --- blocked 占用活跃尝试 ----------------------------------------------

    def test_blocked_attempt_occupies_the_session_slot(self):
        self.ticket()
        self.running(session="executor-1")
        self.ok("ask_ticket", self.ask_payload(self.ok("get_ticket", self.scope())["current_attempt"]))
        self.ticket("t2")
        # 这里验的是会话槽位，不是项目串行；显式越过串行（AW-061）。
        claim = self.ok("claim_ticket", self.scope(ticket_id="t2", expected_revision=1,
                                                  coordinator_session_id="coordinator-2",
                                                  allow_parallel=True))
        # 等答案的会话不能再去领第二张单：不然答案回来时同一 session 会有两条 running 尝试。
        self.error("CONFLICT", "bind_session", self.scope(
            ticket_id="t2", attempt_id=claim["attempt_id"], session_id="executor-1",
            project_root=str(self.project)))
        self.assertEqual("dispatching", self.ok("get_ticket", self.scope(ticket_id="t2"))["state"])
        # 换一个会话就能绑：被约束的是会话，不是工单。
        self.assertTrue(self.call("bind_session", self.scope(
            ticket_id="t2", attempt_id=claim["attempt_id"], session_id="executor-2",
            project_root=str(self.project)))["ok"])

    def test_recover_frees_the_session_slot(self):
        self.ticket()
        attempt, _ = self.blocked()
        self.ticket("t2")
        # 同上：验的是会话槽位，不是项目串行（AW-061）。
        claim = self.ok("claim_ticket", self.scope(ticket_id="t2", expected_revision=1,
                                                  coordinator_session_id="coordinator-2",
                                                  allow_parallel=True))
        self.error("CONFLICT", "bind_session", self.scope(
            ticket_id="t2", attempt_id=claim["attempt_id"], session_id="executor-1",
            project_root=str(self.project)))
        self.ok("recover_ticket", self.scope(attempt_id=attempt, expected_revision=1, reason="收回"))
        self.assertTrue(self.call("bind_session", self.scope(
            ticket_id="t2", attempt_id=claim["attempt_id"], session_id="executor-1",
            project_root=str(self.project)))["ok"])

    # --- 谁能答 ------------------------------------------------------------

    def test_coordinator_may_answer_on_behalf_of_the_human(self):
        self.ticket()
        _, question = self.blocked()
        receipt = self.ok("answer_ticket", {**self.answer_payload(question),
                                            "answered_by": "coordinator",
                                            "answered_by_session_id": "coordinator-1"})
        self.assertEqual("coordinator", receipt["answered_by"])
        self.assertEqual("coordinator-1",
                         self.ok("get_ticket", self.scope())["answers"][0]["answered_by_session_id"])

    def test_other_session_cannot_answer_as_the_coordinator(self):
        self.ticket()
        _, question = self.blocked()
        payload = {**self.answer_payload(question), "answered_by": "coordinator",
                   "answered_by_session_id": "coordinator-2"}
        self.error("NOT_COORDINATOR", "answer_ticket", payload)

    def test_executing_session_cannot_answer_its_own_question(self):
        self.ticket()
        _, question = self.blocked()
        payload = {**self.answer_payload(question), "answered_by": "coordinator",
                   "answered_by_session_id": "executor-1"}
        self.error("SELF_ANSWER", "answer_ticket", payload)

    def test_human_answer_must_not_carry_a_session_id(self):
        self.ticket()
        _, question = self.blocked()
        self.error("INVALID_INPUT", "answer_ticket",
                   {**self.answer_payload(question), "answered_by_session_id": "coordinator-1"})
        self.error("INVALID_INPUT", "answer_ticket",
                   {**self.answer_payload(question), "answered_by_session_id": None})
        self.error("INVALID_INPUT", "answer_ticket",
                   {**self.answer_payload(question), "answered_by": "someone"})
        self.assertEqual("blocked", self.ok("get_ticket", self.scope())["state"])

    # --- 字段与错误码 ------------------------------------------------------

    def test_ask_rejects_bad_shapes_and_foreign_sessions(self):
        self.ticket()
        attempt = self.running()
        self.error("SESSION_MISMATCH", "ask_ticket", self.ask_payload(attempt, session="executor-2"))
        self.error("PROJECT_MISMATCH", "ask_ticket", {**self.ask_payload(attempt), "project_id": "p2"})
        self.error("STALE_ATTEMPT", "ask_ticket", {**self.ask_payload(attempt), "attempt_id": "nope"})
        for bad in ("", "   ", "x" * 2001, 7):
            self.error("INVALID_INPUT", "ask_ticket", {**self.ask_payload(attempt), "question": bad})
        self.error("INVALID_INPUT", "ask_ticket", {**self.ask_payload(attempt), "note": "多传字段"})
        self.error("INVALID_INPUT", "ask_ticket", {**self.ask_payload(attempt),
                                                  "options": [{"id": "A", "text": "a"}] * 7})
        self.error("INVALID_INPUT", "ask_ticket", {**self.ask_payload(attempt),
                                                  "options": [{"id": "A", "text": "a"}, {"id": "A", "text": "b"}]})
        self.error("INVALID_INPUT", "ask_ticket", {**self.ask_payload(attempt),
                                                  "options": [{"id": "A"}]})
        self.error("INVALID_INPUT", "ask_ticket", {**self.ask_payload(attempt),
                                                  "options": [{"id": "x" * 21, "text": "a"}]})
        self.error("INVALID_INPUT", "ask_ticket", {**self.ask_payload(attempt),
                                                  "options": [{"id": "A", "text": "x" * 201}]})
        # 全部被拒之后工单还在 running，一次都没进 blocked。
        self.assertEqual("running", self.ok("get_ticket", self.scope())["state"])
        self.assertEqual([], self.ok("get_ticket", self.scope())["questions"])

    def test_ask_rejects_tickets_that_are_not_running(self):
        self.ticket()
        claim = self.ok("claim_ticket", self.scope(expected_revision=1, coordinator_session_id="coordinator-1"))
        # dispatching 的尝试还没有绑会话，所以先抦的是会话不匹配。
        self.error("SESSION_MISMATCH", "ask_ticket", self.ask_payload(claim["attempt_id"], session="executor-1"))
        self.ok("bind_session", self.scope(attempt_id=claim["attempt_id"], session_id="executor-1",
                                           project_root=str(self.project)))
        self.ok("submit_result", self.result_payload(claim["attempt_id"]))
        self.error("INVALID_STATE", "ask_ticket", self.ask_payload(claim["attempt_id"]))

    def test_answer_rejects_unknown_question_project_and_stale_prd(self):
        self.ticket()
        _, question = self.blocked()
        self.error("QUESTION_MISMATCH", "answer_ticket", self.answer_payload("q-unknown"))
        self.error("PROJECT_MISMATCH", "answer_ticket",
                   {**self.answer_payload(question), "project_id": "p2"})
        for bad in ("", "abc", "z" * 64, 7):
            self.error("INVALID_INPUT", "answer_ticket", {**self.answer_payload(question), "prd_sha256": bad})
        self.error("INVALID_INPUT", "answer_ticket", {**self.answer_payload(question), "note": "多传字段"})
        self.error("INVALID_INPUT", "answer_ticket",
                   {k: v for k, v in self.answer_payload(question).items() if k != "answer"})
        # 需求在等待期间被改过：答案依据的版本不再是工单当前版本。
        self.error("STALE_PRD", "answer_ticket",
                   {**self.answer_payload(question), "prd_sha256": "0" * 64})
        self.assertEqual("blocked", self.ok("get_ticket", self.scope())["state"])
        # 写操作都要 request_id：没有它连租约都记不下来。
        self.assertEqual("INVALID_INPUT",
                         self.center.execute("answer_ticket", self.answer_payload(question))["error"]["code"])

    def test_answer_rejects_question_open_while_ticket_is_not_blocked(self):
        self.ticket()
        attempt = self.running()
        # 不存在的编号先报「不匹配」，而不是「状态不对」。
        self.error("QUESTION_MISMATCH", "answer_ticket", self.answer_payload("q-anything"))
        # 兜底路径：open 问题配着 running 工单（正常写不进这个组合）。
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("INSERT INTO questions VALUES ('q-1','t1',?,'问','[]','open','2026-01-01T00:00:00+00:00')",
                         (attempt,))
            conn.commit()
        finally:
            conn.close()
        self.error("INVALID_STATE", "answer_ticket", self.answer_payload("q-1"))
        self.assertEqual("running", self.ok("get_ticket", self.scope())["state"])
        self.assertEqual([], self.ok("get_ticket", self.scope())["answers"])

    # --- schema 6 ----------------------------------------------------------

    def test_schema_six_has_question_tables_and_a_blocked_wide_index(self):
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(8, conn.execute("PRAGMA user_version").fetchone()[0])
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertLessEqual({"questions", "answers"}, tables)
            indexes = {row[0]: row[1] for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND name IN ('active_session','one_open_question')")}
            self.assertIn("'blocked'", indexes["active_session"])
            self.assertIn("state='open'", indexes["one_open_question"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
