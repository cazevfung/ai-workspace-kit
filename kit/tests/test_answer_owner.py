"""AW-122：`answer_ticket` 的授权也要读 owner，不能只有 `record_notice` 读了。

现场是 AW-118：派单器（`[Schedule] AI工作区无人值守派单器`，`d4290a0f`）claim 之后
宿主把它归档了，于是 `answer_ticket` 对任何人都是 `NOT_COORDINATOR`——它那时读的是
`attempts.coordinator_session_id`（origin，永不改写），而能接手的人已经换成了
`tickets.owner_session_id`。同一张单上出现了两把尺：回执读 owner，代答读 origin。

这一批用例判的就是「一把尺」：

1. 未接替时（owner 此刻等于 origin）协调会话照旧能代答，别的会话照旧被拒——
   行为与以前逐字一致，没有为了让新路通而放宽旧路。
2. `take_over_ticket` 之后，新 owner 能代答，原 origin 不能再代答。
3. 执行会话仍不能答自己提的问题（`SELF_ANSWER` 不随 owner 变），
   `human` 代答仍不带会话标识（`answered_by_session_id` 仍是 coordinator 专属）。

接替本身是自报声明，这里不认证它；判的只是「口径是不是只剩一把尺」。
"""
import tempfile
import unittest
from pathlib import Path

from task_center import Center

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "89abcdef0123456789abcdef0123456789abcdef"


class AnswerOwnerTest(unittest.TestCase):
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
        payload = payload or {}
        alias = payload.get("ticket_id")
        if isinstance(alias, str) and alias in self.ids:
            payload = {**payload, "ticket_id": self.ids[alias]}
        return self.center.execute(op, payload, request_id or f"req-{self.counter}")

    def ok(self, op, payload=None):
        result = self.call(op, payload)
        self.assertTrue(result["ok"], result)
        return result["result"]

    def error(self, code, op, payload=None):
        result = self.call(op, payload)
        self.assertFalse(result["ok"], result)
        self.assertEqual(code, result["error"]["code"], result)
        return result

    def scope(self, **extra):
        return {"project_id": "p1", "ticket_id": "t1", **extra}

    def blocked(self, coordinator="coordinator-1", executor="executor-1"):
        """一张开了工、提了问题、正等一句话的单；返回问题编号。"""
        created = self.ok("create_ticket", {
            "project_id": "p1", "title": "整理检查清单",
            "deliverable": "一份逐项可勾选的检查清单",
            "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        })
        self.ids["t1"] = created["id"]
        claim = self.ok("claim_ticket", self.scope(expected_revision=1,
                                                  coordinator_session_id=coordinator))
        self.ok("bind_session", self.scope(attempt_id=claim["attempt_id"], session_id=executor,
                                          project_root=str(self.project)))
        return self.ok("ask_ticket", {
            **self.scope(), "attempt_id": claim["attempt_id"], "session_id": executor,
            "question": "这批差异算缺陷还是口径问题？",
        })["question_id"], claim["attempt_id"]

    def answer_payload(self, question_id, answerer="coordinator-1", **extra):
        payload = self.scope(question_id=question_id,
                             answer="算口径问题，改文档，不改代码",
                             answered_by="coordinator",
                             prd_sha256=self.ok("get_ticket", self.scope())["prd_sha256"])
        if answerer is not None:
            payload["answered_by_session_id"] = answerer
        return {**payload, **extra}

    def take_over(self, new_owner="coordinator-2"):
        return self.ok("take_over_ticket", {
            **self.scope(), "new_owner_session_id": new_owner,
            "reason": "派单它的调度会话已经归档（宿主 status=archived），由常驻会话接替",
        })

    # --- 未接替时：行为与以前逐字一致 -------------------------------------

    def test_before_takeover_the_dispatcher_still_answers(self):
        question, _ = self.blocked()
        receipt = self.ok("answer_ticket", self.answer_payload(question))
        self.assertEqual("coordinator", receipt["answered_by"])
        self.assertEqual("coordinator-1",
                         self.ok("get_ticket", self.scope())["answers"][0]["answered_by_session_id"])

    def test_before_takeover_another_session_is_still_refused(self):
        question, _ = self.blocked()
        self.error("NOT_COORDINATOR", "answer_ticket", self.answer_payload(question, answerer="coordinator-2"))

    # --- 接替之后：一把尺，读 owner ---------------------------------------

    def test_after_takeover_the_new_owner_answers(self):
        question, _ = self.blocked()
        self.take_over()
        receipt = self.ok("answer_ticket", self.answer_payload(question, answerer="coordinator-2"))
        self.assertEqual("coordinator", receipt["answered_by"])
        ticket = self.ok("get_ticket", self.scope())
        self.assertEqual("running", ticket["state"])
        self.assertEqual("coordinator-2", ticket["owner_session_id"])
        # origin 不动：谁派的这条尝试仍是历史事实。
        self.assertEqual("coordinator-1", ticket["attempts"][-1]["coordinator_session_id"])
        self.assertEqual("coordinator-2", ticket["answers"][-1]["answered_by_session_id"])

    def test_after_takeover_the_origin_can_no_longer_answer(self):
        question, _ = self.blocked()
        self.take_over()
        self.error("NOT_COORDINATOR", "answer_ticket", self.answer_payload(question, answerer="coordinator-1"))
        # 被拒时一行不写：问题仍开着，工单仍在等这句话。
        ticket = self.ok("get_ticket", self.scope())
        self.assertEqual("blocked", ticket["state"])
        self.assertEqual([], ticket["answers"])

    def test_the_executing_session_still_cannot_answer_its_own_question(self):
        question, _ = self.blocked()
        self.take_over()
        self.error("SELF_ANSWER", "answer_ticket", self.answer_payload(question, answerer="executor-1"))

    def test_human_answers_still_carry_no_session_id(self):
        question, _ = self.blocked()
        self.take_over()
        self.error("INVALID_INPUT", "answer_ticket",
                   self.answer_payload(question, answerer=None, answered_by="human",
                                       answered_by_session_id="coordinator-2"))
        receipt = self.ok("answer_ticket", self.answer_payload(question, answerer=None, answered_by="human"))
        self.assertEqual("human", receipt["answered_by"])
        self.assertEqual("running", self.ok("get_ticket", self.scope())["state"])


if __name__ == "__main__":
    unittest.main()
