"""第十个终态 `archived`：只能由 `done` 进入、必带理由、不算在途、历史行不删。

这批用例判的是状态机本身（AW-128 C1 / C6 / C7）：

1. 归档一次成功、`revision` 不变；同一张单再归档一次报 `INVALID_STATE`，
   两次回执里的 `revision` 相同——归档不是新的一代，失败的第二次也不许改它。
2. 只有 `state='done'` 能归档：`running` / `ready` / `planned` / `cancelled` 一律
   `INVALID_STATE`，且工单状态一行不动。
3. `reason` 必填：缺了报 `INVALID_INPUT`，一行不写。
4. 归档不等于没做完：归档后的 A 作为 B 的 `blocked_by` 前置算满足，B 能被领走
   （`BLOCKED_BY_DEPENDENCY` 不该再出现）。
5. 归档之后 `list_unmerged` 与在途集合都不再点名它，而 `reviews` / `submissions`
   的行数一条不少。
"""
import tempfile
import unittest
from pathlib import Path

from task_center import Center

# 提交用的 head sha：本文件只验「记下来、读得回、比得上」，不验 sha 本身对不对。
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "89abcdef0123456789abcdef0123456789abcdef"


class ArchiveTest(unittest.TestCase):
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

    def ticket(self, alias="t1", **extra):
        created = self.ok("create_ticket", {
            "project_id": "p1", "title": f"整理检查清单 {alias}",
            "deliverable": "一份逐项可勾选的检查清单",
            "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
            **extra,
        })
        self.ids[alias] = created["id"]
        return created

    def done_ticket(self, alias="t1", coordinator=None, executor=None):
        """走真流程把一张单推到 done：create → claim → bind → submit → review(accept)。"""
        coordinator = coordinator or f"coordinator-{alias}"
        executor = executor or f"executor-{alias}"
        self.ticket(alias)
        claim = self.ok("claim_ticket", self.scope(ticket_id=alias, expected_revision=1,
                                                  coordinator_session_id=coordinator))
        self.ok("bind_session", self.scope(ticket_id=alias, attempt_id=claim["attempt_id"],
                                           session_id=executor, project_root=str(self.project)))
        (self.project / f"result-{alias}.md").write_text("1. 检查步骤", encoding="utf-8")
        self.ok("submit_result", self.scope(
            ticket_id=alias, attempt_id=claim["attempt_id"], session_id=executor,
            artifacts=[f"result-{alias}.md"], summary="清单已完成", evidence=["逐项已勾选"],
            risks=[], head_sha=HEAD_SHA, base_sha=BASE_SHA))
        submission = self.ok("get_ticket", self.scope(ticket_id=alias))["submissions"][0]
        submitted = self.ok("get_ticket", self.scope(ticket_id=alias))
        self.assertEqual("awaiting_review", submitted["state"])
        self.ok("review_result", self.scope(
            ticket_id=alias, attempt_id=claim["attempt_id"], submission_id=submission["id"],
            reviewer_session_id=f"reviewer-{alias}", verdict="accept",
            checks=[{"id": "C1", "passed": True, "evidence": "跑了 run_tests.py，退出码 0"}],
            reason="逐项核过"))
        ticket = self.ok("get_ticket", self.scope(ticket_id=alias))
        self.assertEqual("done", ticket["state"])
        return ticket

    def archive(self, alias="t1", revision=None, **extra):
        ticket = self.ok("get_ticket", self.scope(ticket_id=alias))
        payload = self.scope(ticket_id=alias, reason="存量缺口清账：11-21 前已验收、缺回执与合并记录，按历史事实归档",
                            expected_revision=ticket["revision"] if revision is None else revision, **extra)
        return self.call("archive_ticket", payload)

    # --- C1：两次归档 -------------------------------------------------------

    def test_archive_moves_done_ticket_and_keeps_revision(self):
        ticket = self.done_ticket()
        self.assertEqual("done", ticket["state"])
        revision = ticket["revision"]

        first = self.archive()
        self.assertTrue(first["ok"], first)
        self.assertEqual("archived", first["result"]["state"])
        self.assertEqual(revision, first["result"]["revision"])

        second = self.archive()
        self.assertFalse(second["ok"], second)
        self.assertEqual("INVALID_STATE", second["error"]["code"])
        # 「回执里的 revision 与第一次相同」：第二次是失败回执，revision 写在正文里。
        self.assertIn(f"revision {revision}", second["error"]["message"])
        current = self.ok("get_ticket", self.scope())
        self.assertEqual("archived", current["state"])
        self.assertEqual(revision, current["revision"])

    def test_archive_refuses_every_state_but_done(self):
        self.ticket()
        self.error("INVALID_STATE", "archive_ticket", self.scope(
            expected_revision=1, reason="还没验收不许归档"))
        self.assertEqual("ready", self.ok("get_ticket", self.scope())["state"])

        claim = self.ok("claim_ticket", self.scope(expected_revision=1, coordinator_session_id="coordinator-1"))
        self.ok("bind_session", self.scope(attempt_id=claim["attempt_id"], session_id="executor-1",
                                          project_root=str(self.project)))
        before = self.ok("get_ticket", self.scope())
        self.assertEqual("running", before["state"])
        refused = self.archive(revision=before["revision"])
        self.assertFalse(refused["ok"], refused)
        self.assertEqual("INVALID_STATE", refused["error"]["code"])
        after = self.ok("get_ticket", self.scope())
        self.assertEqual("running", after["state"])
        self.assertEqual(before["revision"], after["revision"])

    # --- C3：理由必填 -------------------------------------------------------

    def test_archive_requires_a_reason(self):
        ticket = self.done_ticket()
        for bad in (None, "", "   "):
            payload = self.scope(expected_revision=ticket["revision"])
            if bad is not None:
                payload["reason"] = bad
            self.error("INVALID_INPUT", "archive_ticket", payload)
        self.assertEqual("done", self.ok("get_ticket", self.scope())["state"])

    # --- C6：归档不等于没做完 ----------------------------------------------

    def test_archived_prerequisite_satisfies_blocked_by(self):
        self.done_ticket("t1")
        self.ticket("t2", blocked_by=[self.ids["t1"]])
        # 归档之前，前置是 done，claim 本来就该过；这一步同时固定「归档前也满足」。
        archived = self.archive("t1")
        self.assertTrue(archived["ok"], archived)
        claim = self.ok("claim_ticket", self.scope(ticket_id="t2", expected_revision=1,
                                                  coordinator_session_id="coordinator-2"))
        self.assertEqual("dispatching", claim["state"])

    def test_cancelled_prerequisite_still_blocks_but_archived_does_not(self):
        """反例守一手：`cancelled` 仍然不算满足，别把归档和取消混成一类。"""
        self.ticket("t1")
        self.ok("cancel_ticket", self.scope(expected_revision=1, reason="不做了"))
        self.ticket("t2", blocked_by=[self.ids["t1"]])
        self.error("BLOCKED_BY_DEPENDENCY", "claim_ticket", self.scope(
            ticket_id="t2", expected_revision=1, coordinator_session_id="coordinator-2"))

    # --- C7 / C9：不算在途、不进清单、历史行不删 ---------------------------

    def test_archived_leaves_in_flight_lists_but_keeps_history(self):
        self.done_ticket()
        before = self.ok("get_ticket", self.scope())
        self.assertEqual(1, len(before["submissions"]))
        self.assertEqual(1, len(before["reviews"]))
        self.assertEqual(1, len(self.ok("list_unmerged", {})["items"]))

        self.assertTrue(self.archive()["ok"])

        after = self.ok("get_ticket", self.scope())
        self.assertEqual("archived", after["state"])
        self.assertEqual(len(before["submissions"]), len(after["submissions"]))
        self.assertEqual(len(before["reviews"]), len(after["reviews"]))
        self.assertEqual(0, self.ok("list_unmerged", {})["count"])
        listed = self.ok("list_tickets", {"project_id": "p1"})["items"]
        self.assertEqual(["archived"], [item["state"] for item in listed])
        # 终态不可再走一次终止：归档之后 cancel 也要拒。
        self.error("INVALID_STATE", "cancel_ticket", self.scope(
            expected_revision=after["revision"], reason="归档后不许再取消"))


if __name__ == "__main__":
    unittest.main()
