#!/usr/bin/env python
"""`tools/pool_report.py` 的用例：一条命令就能记账，缺编号或非空判据时不许装成记上了。

判据只看三样东西：退出码、stdout 那一行里的工单号与轮次、以及库里多出来的事件行数。
**不看实现细节**（拼了几次 payload、调了哪个函数），因为那些换一次重构就全变，而
「一轮一条记录」「没编号不许写」是这个脚本存在的全部理由。

跑法：

    python tools/test_pool_report.py     # 单独跑，用例名逐条打印
    python tools/run_tests.py            # 随主线套件跑
"""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
if str(TOOLS.parent) not in sys.path:
    sys.path.insert(0, str(TOOLS.parent))

import pool_report
from task_center import Center
from task_center.core import POOL_REPORT

CRITERIA_ONE = [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}]


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = pool_report.main(argv)
    return code, out.getvalue(), err.getvalue()


class PoolReportToolTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        project = self.root / "项目一"
        project.mkdir()
        (project / "PRD.md").write_text("需求第一版", encoding="utf-8")
        self.db = self.root / "task-center.sqlite3"
        self.center = Center(self.db)
        self.center.execute("init", {}, "req-init")
        self.center.execute("register_project",
                            {"project_id": "p1", "name": "项目一", "root": str(project)},
                            "req-register")

    def ticket(self, criteria=None):
        response = self.center.execute("create_ticket", {
            "project_id": "p1", "title": "先记下来的念头", "deliverable": "待定",
            "prd_path": "PRD.md", "criteria": CRITERIA_ONE if criteria else [],
            "state": "planned",
        }, f"req-create-{1 if criteria else 0}")
        self.assertTrue(response["ok"], response)
        return response["result"]["id"]

    def events(self, ticket_id):
        response = self.center.execute("events", {"project_id": "p1", "ticket_id": ticket_id,
                                                  "limit": 100})
        self.assertTrue(response["ok"], response)
        return [row for row in response["result"]["items"]
                if row["operation"] == POOL_REPORT and "result" in (row["detail"] or {})]

    def test_records_a_round(self):
        ticket_id = self.ticket()
        code, out, _ = run_main(["--db", str(self.db), "--project", "p1", "--ticket", ticket_id,
                                 "--request-id", "pool-1", "--session-id", "patrol-1"])
        self.assertEqual(code, 0)
        self.assertIn(ticket_id, out)
        self.assertIn("第 1 轮", out)
        self.assertEqual(len(self.events(ticket_id)), 1)

    def test_rounds_are_counted_up(self):
        ticket_id = self.ticket()
        first = run_main(["--db", str(self.db), "--project", "p1", "--ticket", ticket_id,
                          "--request-id", "pool-a"])
        second = run_main(["--db", str(self.db), "--project", "p1", "--ticket", ticket_id,
                           "--request-id", "pool-b"])
        self.assertEqual(first[0], 0)
        self.assertEqual(second[0], 0)
        self.assertIn("第 2 轮", second[1])

    def test_missing_request_id_is_refused(self):
        """没有编号就没法幂等：重试会把同一轮记成两轮，所以当场拒绝、一行不写。"""
        ticket_id = self.ticket()
        code, out, err = run_main(["--db", str(self.db), "--project", "p1", "--ticket", ticket_id])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--request-id", err)
        self.assertEqual(self.events(ticket_id), [])

    def test_ticket_with_criteria_exits_non_zero(self):
        ticket_id = self.ticket(criteria=True)
        code, out, err = run_main(["--db", str(self.db), "--project", "p1", "--ticket", ticket_id,
                                   "--request-id", "pool-c"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("INVALID_STATE", err)
        self.assertEqual(self.events(ticket_id), [])

    def test_json_prints_the_receipt(self):
        import json
        ticket_id = self.ticket()
        code, out, _ = run_main(["--db", str(self.db), "--project", "p1", "--ticket", ticket_id,
                                 "--request-id", "pool-d", "--json"])
        self.assertEqual(code, 0)
        receipt = json.loads(out)
        self.assertEqual(receipt["result"]["reported_rounds"], 1)


if __name__ == "__main__":
    unittest.main()
