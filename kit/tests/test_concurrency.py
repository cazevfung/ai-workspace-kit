"""AC09：两个项目里的独立任务真的同时在做，且不混用材料。

上一轮这条只有「一个会话不能同时领两个工单」的单点约束，没有任何证据证明
两个项目能并行。这里的做法不是「建了两条会话」，而是：

1. 两个线程各自 `claim_ticket → bind_session`，用一个 Barrier 卡在两条尝试都处于
   running 的那一刻；
2. 用事件表里 `bind_session` 与 `submit_result` 的时间戳算出两条尝试的实际区间，
   断言区间确实重叠；
3. 再断言材料不跨界：跨项目查工单报 PROJECT_MISMATCH，引用另一个项目的文件报
   PATH_OUTSIDE_PROJECT，同一个会话不能同时绑两个项目的工单。
"""
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from task_center import Center


def moment(value):
    return datetime.fromisoformat(value)


class DualProjectConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.alpha = self.root / "项目甲"
        self.beta = self.root / "项目乙"
        for path in (self.alpha, self.beta):
            path.mkdir()
            (path / "PRD.md").write_text("需求第一版", encoding="utf-8")
        self.db = self.root / "center.sqlite3"
        self.center = Center(self.db)
        self.counter = 0
        self.ok(self.center, "init")
        for pid, name, path in (("alpha", "甲", self.alpha), ("beta", "乙", self.beta)):
            self.ok(self.center, "register_project", {"project_id": pid, "name": name, "root": str(path)})
            self.ok(self.center, "create_ticket", {
                "ticket_id": f"{pid}-t1", "project_id": pid, "title": "整理检查清单",
                "deliverable": "一份逐项可勾选的检查清单", "prd_path": "PRD.md",
                "criteria": [{"id": "C1", "text": "包含明确的检查步骤"}],
            })

    def call(self, center, operation, payload=None, request_id=None):
        self.counter += 1
        return center.execute(operation, payload or {}, request_id or f"req-{self.counter}")

    def ok(self, center, operation, payload=None, request_id=None):
        result = self.call(center, operation, payload, request_id)
        self.assertTrue(result["ok"], result)
        return result["result"]

    def events(self, pid, tid):
        return self.ok(self.center, "events", {"project_id": pid, "ticket_id": tid})["items"]

    def attempt_window(self, pid, tid):
        """尝试的实际区间：绑定那一刻到提交那一刻，都取自事件表而不是调用方自报。"""
        stamps = {}
        for item in self.events(pid, tid):
            if item["operation"] == "bind_session":
                stamps["start"] = moment(item["created_at"])
            if item["operation"] == "submit_result":
                stamps["end"] = moment(item["created_at"])
        self.assertEqual({"start", "end"}, set(stamps), f"{tid} 缺少绑定或提交事件")
        return stamps["start"], stamps["end"]

    def parallel_submit(self, plan):
        """plan: [(pid, tid, session, artifact_text)]，两条尝试同时停在 running。"""
        barrier = threading.Barrier(len(plan), timeout=20)
        results, errors = {}, []

        def worker(pid, tid, session, text):
            try:
                center = Center(self.db)
                root = self.alpha if pid == "alpha" else self.beta
                claim = self.ok(center, "claim_ticket",
                                {"project_id": pid, "ticket_id": tid, "expected_revision": 1,
                                 "coordinator_session_id": f"coord-{pid}"})
                self.ok(center, "bind_session", {
                    "project_id": pid, "ticket_id": tid, "attempt_id": claim["attempt_id"],
                    "session_id": session, "project_root": str(root),
                })
                barrier.wait()  # 两条尝试此刻都在 running
                (root / "result.md").write_text(text, encoding="utf-8")
                results[tid] = self.ok(center, "submit_result", {
                    "project_id": pid, "ticket_id": tid, "attempt_id": claim["attempt_id"],
                    "session_id": session, "artifacts": ["result.md"],
                    "summary": f"{tid} 的清单", "evidence": ["按 PRD 逐条核对"], "risks": [],
                })
            except BaseException as exc:  # noqa: BLE001 - 收集到主线程再断言，避免线程里静默失败
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=item) for item in plan]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual([], errors)
        self.assertEqual(len(plan), len(results))
        return results

    def test_two_projects_run_in_overlapping_windows_without_sharing_material(self):
        self.parallel_submit([("alpha", "alpha-t1", "exec-alpha", "甲的清单"),
                              ("beta", "beta-t1", "exec-beta", "乙的清单")])
        a_start, a_end = self.attempt_window("alpha", "alpha-t1")
        b_start, b_end = self.attempt_window("beta", "beta-t1")
        overlap = min(a_end, b_end) - max(a_start, b_start)
        self.assertGreater(overlap.total_seconds(), 0,
                           f"两条尝试没有重叠：甲 {a_start}→{a_end}，乙 {b_start}→{b_end}")

        alpha_ticket = self.ok(self.center, "get_ticket", {"project_id": "alpha", "ticket_id": "alpha-t1"})
        beta_ticket = self.ok(self.center, "get_ticket", {"project_id": "beta", "ticket_id": "beta-t1"})
        self.assertEqual("awaiting_review", alpha_ticket["state"])
        self.assertEqual("awaiting_review", beta_ticket["state"])
        self.assertEqual("exec-alpha", alpha_ticket["attempts"][0]["session_id"])
        self.assertEqual("exec-beta", beta_ticket["attempts"][0]["session_id"])
        self.assertEqual(["result.md"], [item["path"] for item in alpha_ticket["submissions"][0]["artifacts"]])
        self.assertEqual(["result.md"], [item["path"] for item in beta_ticket["submissions"][0]["artifacts"]])
        # 两边都叫 result.md，但摘要不同——证明它们读的是各自项目里的那份文件。
        self.assertNotEqual(alpha_ticket["submissions"][0]["artifacts"][0]["sha256"],
                            beta_ticket["submissions"][0]["artifacts"][0]["sha256"])

    def test_one_session_cannot_hold_two_projects_at_once(self):
        claim = self.ok(self.center, "claim_ticket",
                        {"project_id": "alpha", "ticket_id": "alpha-t1", "expected_revision": 1,
                         "coordinator_session_id": "coord-alpha"})
        self.ok(self.center, "bind_session", {
            "project_id": "alpha", "ticket_id": "alpha-t1", "attempt_id": claim["attempt_id"],
            "session_id": "shared-session", "project_root": str(self.alpha),
        })
        other = self.ok(self.center, "claim_ticket",
                        {"project_id": "beta", "ticket_id": "beta-t1", "expected_revision": 1,
                         "coordinator_session_id": "coord-beta"})
        result = self.call(self.center, "bind_session", {
            "project_id": "beta", "ticket_id": "beta-t1", "attempt_id": other["attempt_id"],
            "session_id": "shared-session", "project_root": str(self.beta),
        })
        self.assertFalse(result["ok"], result)
        self.assertEqual("CONFLICT", result["error"]["code"])

    def test_material_from_another_project_is_rejected(self):
        claim = self.ok(self.center, "claim_ticket",
                        {"project_id": "beta", "ticket_id": "beta-t1", "expected_revision": 1,
                         "coordinator_session_id": "coord-beta"})
        self.ok(self.center, "bind_session", {
            "project_id": "beta", "ticket_id": "beta-t1", "attempt_id": claim["attempt_id"],
            "session_id": "exec-beta", "project_root": str(self.beta),
        })
        (self.alpha / "alpha-only.md").write_text("只属于甲的材料", encoding="utf-8")
        payload = {
            "project_id": "beta", "ticket_id": "beta-t1", "attempt_id": claim["attempt_id"],
            "session_id": "exec-beta", "summary": "引用别项目材料", "evidence": ["读了别的项目"],
            "risks": [],
        }
        for artifact in (str(self.alpha / "alpha-only.md"), "../项目甲/alpha-only.md"):
            result = self.call(self.center, "submit_result", {**payload, "artifacts": [artifact]})
            self.assertFalse(result["ok"], result)
            self.assertEqual("PATH_OUTSIDE_PROJECT", result["error"]["code"])
        result = self.call(self.center, "get_ticket", {"project_id": "beta", "ticket_id": "alpha-t1"})
        self.assertEqual("PROJECT_MISMATCH", result["error"]["code"])


if __name__ == "__main__":
    unittest.main()
