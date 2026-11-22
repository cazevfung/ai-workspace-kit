"""PRD-013 的三条闸门：条件形状（R1）、并行声明（R2）、拒绝留痕（R3）。

依据 `tasks/plan-014-工单机制的执行面.md` 的任务 B 与 `PRD/PRD-013` 的 AC01–AC05。
这里判四件事，全部断言错误码与库里的事实，不断言提示的措辞（PRD-013 C8 的反例）：

1. 既无命令也不是判断类的条件被拒（`CRITERIA_NOT_JUDGEABLE`），且一行都不落库；
   回执点名是第几条，不是只说「有一条不合格」。
2. 判断类条件必须写反例：同一句话带上「反例」放行，去掉就拒。
3. `allow_parallel: true` 必须给 `parallel_reason`，缺了 `PARALLEL_REASON_REQUIRED`
   且工单停在 `ready`；给了理由与 `parallel_scope` 就放行，两项都进事件。
4. 被拒的写操作留一条 `events` 行：`detail` 只有 `rejected` 与 `request_id`，
   不带 payload 全文；`requests` 里不落行，用同一编号重试仍会重新判定。
5. 历史不回溯：库里已有的裸句条件不因为闸门上线而被重判。

为什么要有这一组：`criteria` 只收两种写法这条口径在 `ticket-dispatch` 与
`prd-authoring` 里都写了，但写在 skill 里就只有读过的人知道；928 条条件里只有
36% 带可执行命令。闸门要落在「当场失败」这一步，而不是更细的提醒。
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from task_center import Center

CRITERIA_GOOD = "含 `python tools/run_tests.py` 且退出码 0"
CRITERIA_JUDGEMENT = "判断类：两份清单口径是否一致；反例：同一数字两处写成不同值"
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"


class JudgeableGateTest(unittest.TestCase):
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

    # --- 夹具 -------------------------------------------------------------

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

    def ticket_payload(self, criteria=None):
        return {
            "project_id": "p1", "title": "整理检查清单",
            "deliverable": "一份逐项可勾选的检查清单",
            "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": CRITERIA_GOOD}] if criteria is None else criteria,
        }

    def ticket(self, alias="t1", criteria=None):
        created = self.ok("create_ticket", self.ticket_payload(criteria))
        self.ids[alias] = created["id"]
        return created

    def scope(self, ticket_id="t1", project_id="p1", **extra):
        return {"project_id": project_id, "ticket_id": ticket_id, **extra}

    def claim_payload(self, ticket_id="t1", revision=1, coordinator="coordinator-1", **extra):
        return self.scope(ticket_id=ticket_id, expected_revision=revision,
                          coordinator_session_id=coordinator, **extra)

    def rows(self, sql, args=()):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute(sql, args)]
        finally:
            conn.close()

    # --- R1 条件形状 -------------------------------------------------------

    def test_plain_sentence_is_rejected(self):
        result = self.error("CRITERIA_NOT_JUDGEABLE", "create_ticket",
                            self.ticket_payload(criteria=[{"id": "C3", "text": "检查一下文档是否准确"}]),
                            "bad-criteria")
        # 点名是第几条：只说「有一条不合格」等于让人自己找。
        self.assertIn("C3", result["error"]["message"])
        # 一行都没写：既不建工单，也不落 requests 回执。
        self.assertEqual([], self.ok("list_tickets")["items"])
        self.assertEqual([], self.rows("SELECT * FROM requests WHERE id='bad-criteria'"))
        # 但拒绝本身留了痕，且 ticket_id 为空（这张单根本不存在）。
        rejected = self.rows("SELECT * FROM events WHERE operation='create_ticket'")
        self.assertEqual(1, len(rejected))
        self.assertIsNone(rejected[0]["ticket_id"])
        self.assertEqual("CRITERIA_NOT_JUDGEABLE", json.loads(rejected[0]["detail"])["rejected"])

    def test_judgement_class_needs_counterexample(self):
        created = self.ticket(criteria=[{"id": "C1", "text": CRITERIA_JUDGEMENT}])
        self.assertEqual("ready", created["state"])
        self.error("CRITERIA_NOT_JUDGEABLE", "create_ticket",
                   self.ticket_payload([{"id": "C1", "text": "判断类：口径一致"}]))
        self.assertEqual([self.tid("t1")], [item["id"] for item in self.ok("list_tickets")["items"]])

    def test_legacy_criteria_not_rejudged(self):
        # 闸门上线前写下的裸句条件：直接落库，模拟历史行；读取时不许被重判。
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                # 列名写全：位置插入在 schema 每加一列时都会静默错位（schema 10 加 criteria_version 时撞过）。
                "INSERT INTO tickets(id,project_id,title,deliverable,prd_path,prd_sha256,criteria,revision,state,"
                "current_attempt,created_at) VALUES ('LEGACY','p1','历史单','一份旧清单','PRD.md',?,"
                "'[{\"id\":\"C1\",\"text\":\"检查一下文档是否准确\"}]',1,'done',NULL,?)",
                ("0" * 64, "2026-01-01T00:00:00+00:00"))
            conn.commit()
        finally:
            conn.close()
        read = self.ok("get_ticket", self.scope("LEGACY"))
        self.assertEqual("检查一下文档是否准确", read["criteria"][0]["text"])
        self.assertIn("LEGACY", [item["id"] for item in self.ok("list_tickets")["items"]])

    # --- R2 并行声明 -------------------------------------------------------

    def test_parallel_requires_reason(self):
        self.ticket("t1")
        self.ticket("t2")
        self.ok("claim_ticket", self.claim_payload("t1"))
        # 缺理由：拒，且工单停在 ready、没有新尝试。
        self.error("PARALLEL_REASON_REQUIRED", "claim_ticket",
                   self.claim_payload("t2", coordinator="coordinator-2", allow_parallel=True))
        stopped = self.ok("get_ticket", self.scope("t2"))
        self.assertEqual("ready", stopped["state"])
        self.assertEqual([], stopped["attempts"])
        # 给理由与范围：放行，两项都进事件，与 parallel_with 并列。
        reason = "两张单动的文件不重叠"
        scope = ["tests/", "docs/protocol.md", "资源:真机"]
        receipt = self.ok("claim_ticket", self.claim_payload(
            "t2", coordinator="coordinator-2", allow_parallel=True,
            parallel_reason=reason, parallel_scope=scope))
        self.assertTrue(receipt["allow_parallel"])
        self.assertEqual(reason, receipt["parallel_reason"])
        overrides = [item for item in self.ok("events", self.scope("t2"))["items"]
                     if item["detail"].get("allow_parallel_override") is True]
        self.assertEqual(1, len(overrides))
        self.assertEqual(reason, overrides[0]["detail"]["parallel_reason"])
        self.assertEqual(scope, overrides[0]["detail"]["parallel_scope"])
        self.assertEqual([{"ticket_id": self.tid("t1"), "state": "dispatching"}],
                         overrides[0]["detail"]["parallel_with"])

    def test_parallel_scope_shape_is_checked(self):
        self.ticket("t1")
        self.ok("claim_ticket", self.claim_payload("t1"))
        self.ticket("t2")
        for bad in ([], [""], ["a"] * 21, "not-a-list", [7]):
            with self.subTest(parallel_scope=bad):
                self.error("INVALID_INPUT", "claim_ticket", self.claim_payload(
                    "t2", coordinator="coordinator-2", allow_parallel=True,
                    parallel_reason="范围形状不对", parallel_scope=bad))

    # --- R3 拒绝留痕 -------------------------------------------------------

    def test_rejected_call_leaves_an_event(self):
        self.ticket("t1")
        payload = self.claim_payload("t1", revision=9)
        self.error("STALE_REVISION", "claim_ticket", payload, "reject-1")
        rows = self.rows(f"SELECT * FROM events WHERE ticket_id='{self.tid('t1')}' ORDER BY id")
        last = rows[-1]
        detail = json.loads(last["detail"])
        self.assertEqual("claim_ticket", last["operation"])
        self.assertEqual("STALE_REVISION", detail["rejected"])
        self.assertEqual("reject-1", detail["request_id"])
        # 不带 payload 全文：整行只有这两个键，正文里没有 payload 的任何字段名。
        self.assertEqual({"rejected", "request_id"}, set(detail))
        self.assertNotIn("summary", last["detail"])
        self.assertNotIn("input", last["detail"])
        # requests 不落行：同一个编号重试会重新判定，拿到的是新的结论而不是旧回执。
        self.assertEqual([], self.rows("SELECT * FROM requests WHERE id='reject-1'"))
        retry = self.error("STALE_REVISION", "claim_ticket", payload, "reject-1")
        self.assertEqual("STALE_REVISION", retry["error"]["code"])
        # 成功路径不变：同一编号换成能过的 payload 就能领取。
        claim = self.ok("claim_ticket", self.claim_payload("t1", revision=1), "reject-1")
        self.assertEqual("dispatching", claim["state"])
        self.assertEqual(1, len([row for row in self.rows("SELECT * FROM requests") if row["id"] == "reject-1"]))

        # ... 成功路径仍然写它自己那条带 result 的事件，两种事件并存。
        details = [json.loads(row["detail"]) for row in self.rows(
            f"SELECT * FROM events WHERE ticket_id='{self.tid('t1')}' ORDER BY id")]
        self.assertTrue(any("rejected" in item for item in details))
        self.assertTrue(any("result" in item for item in details))


if __name__ == "__main__":
    unittest.main()
