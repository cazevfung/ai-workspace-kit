"""AW-140：`record_pool_report` 只落一条可判的事实——「这张空判据单被点名报过一次」。

它补的是 `tools/next_action.py` 缺的那一半：空判据档（`fix_pool`）只报不改，报了几次
没有落库位置，于是同一张单会被一字不变地每轮重报。点名的那一刻要留下一条可查的事实，
读侧才有东西算「该不该再报」「该不该升级」。

这一批用例判四件事：

1. **只落事件，不动状态机。** `state` / `revision` / `criteria_version` / `criteria` 一个
   字节都不变；回执里的 `reported_rounds` 是「记完之后第几轮」。
2. **轮次由行数决定。** 三次调用（三个请求编号）依次是 1、2、3；同一请求号重放不算新一轮。
3. **只给空判据单记账。** 有判据的单、非可领状态（`done` / `cancelled`）都报
   `INVALID_STATE`，一行不写。
4. **拒绝也留痕**：被拒时 `events` 里有一条带 `rejected` 的行，读侧不许把它当成「已报」。
"""
import tempfile
import unittest
from pathlib import Path

from task_center import Center
from task_center.core import POOL_REPORT

CRITERIA_ONE = [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}]


class PoolReportTest(unittest.TestCase):
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
        self.ok("init")
        self.ok("register_project", {"project_id": "p1", "name": "项目一",
                                     "root": str(self.project)})

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

    def blank_ticket(self, title="先记下来的念头"):
        """`planned` 允许 0 条判据：那是唯一能表达「还写不出验收条件」的位置。"""
        return self.ok("create_ticket", {
            "project_id": "p1", "title": title, "deliverable": "待定",
            "prd_path": "PRD.md", "criteria": [], "state": "planned",
        })

    def ticket(self):
        return self.ok("create_ticket", {
            "project_id": "p1", "title": "整理检查清单", "deliverable": "一份清单",
            "prd_path": "PRD.md", "criteria": CRITERIA_ONE,
        })

    def record(self, ticket_id, request_id=None, **extra):
        return self.ok(POOL_REPORT, {"project_id": "p1", "ticket_id": ticket_id, **extra},
                       request_id)

    def view(self, ticket_id):
        return self.ok("get_ticket", {"project_id": "p1", "ticket_id": ticket_id})

    def events(self, operation=POOL_REPORT, ticket_id=None):
        payload = {"project_id": "p1", "limit": 100}
        if ticket_id:
            payload["ticket_id"] = ticket_id
        rows = self.ok("events", payload)["items"]
        return [row for row in rows if row["operation"] == operation]

    # --- 成功路径 ---------------------------------------------------------

    def test_records_one_event_without_touching_the_ticket(self):
        created = self.blank_ticket()
        before = self.view(created["id"])
        receipt = self.record(created["id"], session_id="patrol-1", note="第 1 轮点名")

        self.assertEqual(receipt["ticket_id"], created["id"])
        self.assertEqual(receipt["state"], "planned")
        self.assertEqual(receipt["reported_rounds"], 1)
        self.assertEqual(receipt["session_id"], "patrol-1")
        self.assertEqual(receipt["note"], "第 1 轮点名")

        after = self.view(created["id"])
        for key in ("state", "revision", "criteria_version", "criteria", "title"):
            self.assertEqual(after[key], before[key], key)
        # 「报过」不是新的一代，也不是补了判据：判据仍然空着、工单仍然领不走。
        self.assertEqual(after["criteria"], [])
        self.assertEqual(len(self.events(ticket_id=created["id"])), 1)

    def test_rounds_count_the_events(self):
        created = self.blank_ticket()
        first = self.record(created["id"], request_id="pool-1")
        second = self.record(created["id"], request_id="pool-2")
        third = self.record(created["id"], request_id="pool-3")
        self.assertEqual([first["reported_rounds"], second["reported_rounds"],
                          third["reported_rounds"]], [1, 2, 3])
        self.assertEqual(len(self.events(ticket_id=created["id"])), 3)

    def test_retry_with_the_same_request_id_is_not_a_new_round(self):
        created = self.blank_ticket()
        first = self.record(created["id"], request_id="pool-retry")
        again = self.record(created["id"], request_id="pool-retry")
        self.assertEqual(first["reported_rounds"], again["reported_rounds"])
        self.assertEqual(len(self.events(ticket_id=created["id"])), 1)

    def test_two_tickets_are_counted_separately(self):
        one = self.blank_ticket("念头一")
        two = self.blank_ticket("念头二")
        self.record(one["id"], request_id="pool-a1")
        self.record(one["id"], request_id="pool-a2")
        receipt = self.record(two["id"], request_id="pool-b1")
        self.assertEqual(receipt["reported_rounds"], 1)

    # --- 拒绝路径 ---------------------------------------------------------

    def test_ticket_with_criteria_is_rejected(self):
        created = self.ticket()
        self.error("INVALID_STATE", POOL_REPORT,
                   {"project_id": "p1", "ticket_id": created["id"]})
        # 有判据的单不该背上「空判据报过」的账：只剩那条拒绝行。
        rows = self.events(ticket_id=created["id"])
        self.assertFalse([row for row in rows if "result" in (row["detail"] or {})])
        rejected = [row for row in rows if "rejected" in (row["detail"] or {})]
        self.assertEqual(len(rejected), 1)

    def test_cancelled_ticket_is_rejected(self):
        created = self.blank_ticket()
        revision = self.view(created["id"])["revision"]
        self.ok("cancel_ticket", {"project_id": "p1", "ticket_id": created["id"],
                                  "expected_revision": revision, "reason": "不做了"})
        self.error("INVALID_STATE", POOL_REPORT,
                   {"project_id": "p1", "ticket_id": created["id"]})

    def test_unknown_ticket_is_rejected(self):
        self.error("NOT_FOUND", POOL_REPORT,
                   {"project_id": "p1", "ticket_id": "AW-999999"})

    def test_rejection_row_is_not_a_report(self):
        created = self.ticket()
        self.error("INVALID_STATE", POOL_REPORT,
                   {"project_id": "p1", "ticket_id": created["id"]})
        rows = self.events(ticket_id=created["id"])
        self.assertTrue(rows)
        self.assertTrue(all("rejected" in (row["detail"] or {}) for row in rows))


if __name__ == "__main__":
    unittest.main()
