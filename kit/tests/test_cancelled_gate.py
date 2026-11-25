"""取消是一道单向门：claim / submit / recover 三处续做入口必须报「已取消」（AW-159）。

起因是 2026-11-24 的 HSE-005：14:04:57 取消之后，14:28:38 库里出现了一条
`revision=3` 的尝试 `ae87a2fc`，`events` 里没有对应的 `claim_ticket`；紧接着
14:30:56 的 `submit_result` **成功了**。事后查得到的是「有人直接往 SQLite 里写了」，
查不到的是「他为什么不再试一次工具」——因为工具那时说的是 `STALE_ATTEMPT`
（「这不是当前那次尝试」）与 `STALE_REVISION`（「读取当前状态」），两句都在暗示
「换个 id / 重读一遍就能过」，而取消之后这两条路永远走不通。

所以这张单改的不是「原来能续做、现在不能」（原来也拒），而是**拒绝时说的是哪件事**：

1. 三处都报 `TICKET_CANCELLED`，消息里点名工单号、当前 revision 与唯一出路；
2. 取消这条判据排在 `STALE_REVISION` / `STALE_ATTEMPT` 之前——先报那个让重试
   失去意义的事实，而不是那个会把人送进重试循环的事实；
3. 被拒时一行不写：没有新尝试、revision 不动。

跑法：

    python tools/run_tests.py            # 随主线套件跑
    python -m unittest tests.test_cancelled_gate -v
"""
import tempfile
import unittest
from pathlib import Path

from task_center import Center

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "89abcdef0123456789abcdef0123456789abcdef"


class CancelledGateTest(unittest.TestCase):
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

    def scope(self, ticket_id="t1", **extra):
        return {"project_id": "p1", "ticket_id": ticket_id, **extra}

    def ticket(self, alias="t1"):
        created = self.ok("create_ticket", {
            "project_id": "p1", "title": "整理检查清单",
            "deliverable": "一份逐项可勾选的检查清单",
            "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        })
        self.ids[alias] = created["id"]
        return created

    def claim(self, alias="t1", revision=1, coordinator="coordinator-1"):
        return self.ok("claim_ticket", self.scope(
            alias, expected_revision=revision, coordinator_session_id=coordinator))

    def bind(self, attempt, alias="t1", session="executor-1"):
        return self.ok("bind_session", self.scope(
            alias, attempt_id=attempt, session_id=session, project_root=str(self.project)))

    def submit(self, attempt, alias="t1", session="executor-1"):
        return self.ok("submit_result", self.scope(
            alias, attempt_id=attempt, session_id=session, artifacts=["result.md"],
            summary="清单已完成", evidence=["人工检查条目"], risks=[],
            head_sha=HEAD_SHA, base_sha=BASE_SHA))

    def cancel(self, alias="t1", revision=1):
        return self.ok("cancel_ticket", self.scope(alias, expected_revision=revision, reason="需求取消"))

    def snapshot(self, alias="t1"):
        """被拒前后要一模一样的那几样：状态、revision、尝试条数。"""
        row = self.ok("get_ticket", self.scope(alias))
        return (row["state"], row["revision"], len(row["attempts"]), row["current_attempt"])

    # --- 三处续做入口 -----------------------------------------------------

    def test_cancelled_ticket_cannot_be_claimed(self):
        """claim 在取消之后必须报已取消，且排在 STALE_REVISION 之前。"""
        self.ticket()
        self.cancel()
        before = self.snapshot()
        # 带一个过期 revision 来：报的仍然是「已取消」，不是「重读再来一次」。
        rejected = self.error("TICKET_CANCELLED", "claim_ticket", self.scope(
            expected_revision=1, coordinator_session_id="coordinator-2"))
        message = rejected["error"]["message"]
        self.assertIn(self.tid("t1"), message)
        self.assertIn("cancelled", message)
        self.assertIn("create_ticket", message)
        self.assertEqual(before, self.snapshot())
        self.assertNotIn("claim_ticket", [item["operation"] for item in
                                          self.ok("events", self.scope())["items"] if "result" in item["detail"]])

    def test_cancelled_ticket_cannot_be_submitted(self):
        """submit 在取消之后报已取消，而不是 STALE_ATTEMPT —— 后者听起来像「换个 attempt_id 再试」。"""
        self.ticket()
        attempt = self.claim()["attempt_id"]
        self.bind(attempt)
        self.cancel()
        before = self.snapshot()
        # 拿着取消前那次尝试来提交：这正是 HSE-005 现场 14:30:56 那一步的形状。
        rejected = self.error("TICKET_CANCELLED", "submit_result", self.scope(
            attempt_id=attempt, session_id="executor-1", artifacts=["result.md"],
            summary="清单已完成", evidence=["人工检查条目"], risks=[],
            head_sha=HEAD_SHA, base_sha=BASE_SHA))
        self.assertIn("cancelled", rejected["error"]["message"])
        # 也不许换一个「看起来是当前尝试」的编号蒙过去：取消之后 current_attempt 是空的。
        self.error("TICKET_CANCELLED", "submit_result", self.scope(
            attempt_id="00000000-0000-4000-8000-000000000000", session_id="executor-1",
            artifacts=["result.md"], summary="清单已完成", evidence=["人工检查条目"], risks=[],
            head_sha=HEAD_SHA, base_sha=BASE_SHA))
        self.assertEqual(before, self.snapshot())
        row = self.ok("get_ticket", self.scope())
        self.assertEqual("cancelled", row["state"])
        self.assertEqual([], row["submissions"])

    def test_cancelled_ticket_cannot_be_recovered(self):
        """recover 是「把在飞的尝试作废、退回 ready」：取消之后的单已经没东西可作废。"""
        self.ticket()
        attempt = self.claim()["attempt_id"]
        self.bind(attempt)
        self.cancel()
        before = self.snapshot()
        rejected = self.error("TICKET_CANCELLED", "recover_ticket", self.scope(
            attempt_id=attempt, expected_revision=1, reason="试试能不能退回去"))
        self.assertIn("cancelled", rejected["error"]["message"])
        self.assertEqual(before, self.snapshot())
        self.assertEqual("cancelled", self.ok("get_ticket", self.scope())["state"])

    # --- 反向：门只对着取消，不误伤别的状态 --------------------------------

    def test_running_ticket_still_accepts_claim_submit_recover_paths(self):
        """反例守一手：同一条路径在没取消的单上照旧能走完，别把闸门做成谁都过不去。"""
        self.ticket()
        attempt = self.claim()["attempt_id"]
        self.bind(attempt)
        submitted = self.submit(attempt)
        self.assertEqual("awaiting_review", submitted["state"])
        # 返工之后 recover 也能照常把在飞尝试作废回 ready（这是它存在的理由）。
        self.ok("review_result", self.scope(
            attempt_id=attempt, submission_id=submitted["submission_id"],
            reviewer_session_id="reviewer-1", verdict="reject",
            checks=[{"id": "C1", "status": "fail", "evidence": "清单缺最后一节"}], reason="缺一节"))
        second = self.claim(alias="t1", revision=1)["attempt_id"]
        recovered = self.ok("recover_ticket", self.scope(
            attempt_id=second, expected_revision=1, reason="换人重做"))
        self.assertEqual("ready", recovered["state"])
        self.assertEqual(2, recovered["revision"])


if __name__ == "__main__":
    unittest.main()
