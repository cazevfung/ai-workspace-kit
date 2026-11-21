"""项目登记面：能列出来，登记错了能撤掉，撤不掉时说明原因。

误登记曾经是不可撤销的：`register_project` 没有反操作，全局化之后每个会话都能登记，
代价从「无所谓」变成「永久」。这一组用例锁住撤销路径与它的保护条件。

`RootIsIdentityTest` 那一组锁另一件事：`root` 是身份、`project_id` 是标签
（口径见 `docs/decisions.md`「项目接入面」）——同一 root 重复登记复用原记录且不改标签，开单不要求先登记。
"""
import tempfile
import unittest
from pathlib import Path

from task_center import Center


class RegistryCase(unittest.TestCase):
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
        self.ok("init")
        self.ok("register_project", {"project_id": "alpha", "name": "甲", "root": str(self.alpha)})

    def call(self, operation, payload=None, request_id=None):
        self.counter += 1
        return self.center.execute(operation, payload or {}, request_id or f"req-{self.counter}")

    def ok(self, operation, payload=None, request_id=None):
        result = self.call(operation, payload, request_id)
        self.assertTrue(result["ok"], result)
        return result["result"]

    def error(self, code, operation, payload=None, request_id=None):
        result = self.call(operation, payload, request_id)
        self.assertFalse(result["ok"], result)
        self.assertEqual(code, result["error"]["code"], result)
        return result["error"]

    def ticket(self, ticket_id="t1", project="alpha"):
        root = self.alpha if project == "alpha" else self.beta
        return self.ok("create_ticket", {
            "ticket_id": ticket_id, "project_id": project, "title": "整理检查清单",
            "deliverable": "一份逐项可勾选的检查清单", "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "包含明确的检查步骤"}],
        })

    def ticket_payload(self, ticket_id, project, root=None):
        payload = {
            "ticket_id": ticket_id, "project_id": project, "title": "整理检查清单",
            "deliverable": "一份逐项可勾选的检查清单", "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "包含明确的检查步骤"}],
        }
        if root is not None:
            payload["root"] = str(root)
        return payload

    def labels(self):
        return [(item["project_id"], item["name"]) for item in self.ok("list_projects")["items"]]


class ProjectRegistryTest(RegistryCase):
    def test_list_projects_returns_every_registration(self):
        self.ok("register_project", {"project_id": "beta", "name": "乙", "root": str(self.beta)})
        listed = self.ok("list_projects")
        self.assertEqual({"alpha", "beta"}, {item["project_id"] for item in listed["items"]})
        roots = {Path(item["root"]) for item in listed["items"]}
        self.assertEqual({Path(self.alpha).resolve(), Path(self.beta).resolve()}, roots)

    def test_unregister_frees_the_root_for_a_corrected_registration(self):
        self.ok("unregister_project", {"project_id": "alpha"})
        self.assertEqual([], self.ok("list_projects")["items"])
        self.error("NOT_FOUND", "create_ticket", {
            "ticket_id": "t1", "project_id": "alpha", "title": "任务",
            "deliverable": "一份清单", "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "条件"}],
        })
        # 撤销的意义在于同一个路径能重新登记，且名字可以换掉。
        self.ok("register_project", {"project_id": "alpha-fixed", "name": "甲（修正）", "root": str(self.alpha)})
        self.assertEqual(["alpha-fixed"], [item["project_id"] for item in self.ok("list_projects")["items"]])

    def test_unregister_refuses_while_tickets_refer_to_the_project(self):
        self.ticket()
        error = self.error("PROJECT_HAS_TICKETS", "unregister_project", {"project_id": "alpha"})
        self.assertIn("t1", error["message"])
        self.assertEqual(["alpha"], [item["project_id"] for item in self.ok("list_projects")["items"]])

    def test_unregister_unknown_project_reports_not_found(self):
        self.error("NOT_FOUND", "unregister_project", {"project_id": "ghost"})

    def test_ticket_stays_readable_after_unregistering_an_empty_project(self):
        self.ok("register_project", {"project_id": "beta", "name": "乙", "root": str(self.beta)})
        self.ticket("t1", "alpha")
        self.ok("unregister_project", {"project_id": "beta"})
        self.assertEqual("t1", self.ok("get_ticket", {"project_id": "alpha", "ticket_id": "t1"})["id"])


class RootIsIdentityTest(RegistryCase):
    """`root` 是身份、`project_id` 是标签（口径见 `docs/decisions.md`「项目接入面」）。

    这组用例锁两件事：同一 root 重复登记复用原记录且不改已有标签；开单不要求先登记，
    但已登记的 root 不能被另一张单悄悄换掉。
    """

    def test_same_root_reuses_the_original_record_and_keeps_its_label(self):
        again = self.ok("register_project", {"project_id": "alpha-alias", "name": "别名", "root": str(self.alpha)})
        self.assertEqual("alpha", again["project_id"])
        self.assertEqual("甲", again["name"])
        self.assertTrue(again["reused"])
        # 只有一行，且标签没被第二次调用改掉。
        self.assertEqual([("alpha", "甲")], self.labels())

    def test_repeat_registration_of_every_root_leaves_one_row_each(self):
        for pid, path, name in (("beta", self.beta, "乙"), ("beta-again", self.beta, "别的名字")):
            self.ok("register_project", {"project_id": pid, "name": name, "root": str(path)})
        self.assertEqual([("alpha", "甲"), ("beta", "乙")], self.labels())

    def test_create_ticket_with_root_registers_the_project_on_the_way(self):
        ticket = self.ok("create_ticket", self.ticket_payload("t9", "beta", self.beta))
        self.assertEqual("beta", ticket["project_id"])
        # 名字默认取目录名，不要求调用方另外起一个。
        self.assertEqual([("alpha", "甲"), ("beta", "项目乙")], self.labels())

    def test_create_ticket_without_root_still_reports_not_found(self):
        error = self.error("NOT_FOUND", "create_ticket", self.ticket_payload("t9", "beta"))
        self.assertIn("root", error["message"])
        self.assertEqual([("alpha", "甲")], self.labels())

    def test_create_ticket_with_matching_root_keeps_the_existing_record(self):
        ticket = self.ok("create_ticket", self.ticket_payload("t9", "alpha", self.alpha))
        self.assertEqual("alpha", ticket["project_id"])
        self.assertEqual([("alpha", "甲")], self.labels())

    def test_create_ticket_with_different_root_reports_mismatch_and_changes_nothing(self):
        before = self.ok("list_projects")["items"]
        self.error("ROOT_MISMATCH", "create_ticket", self.ticket_payload("t9", "alpha", self.beta))
        self.assertEqual(before, self.ok("list_projects")["items"])
        self.assertEqual([], self.ok("list_tickets")["items"])

    def test_create_ticket_cannot_claim_a_root_owned_by_another_label(self):
        self.ok("register_project", {"project_id": "beta", "name": "乙", "root": str(self.beta)})
        self.error("ROOT_MISMATCH", "create_ticket", self.ticket_payload("t9", "gamma", self.beta))
        self.assertEqual([("alpha", "甲"), ("beta", "乙")], self.labels())

    def test_same_directory_name_under_two_roots_stays_two_rows(self):
        # Cindy 列表里有六个 `bots/*/workspace`：名字撞车不影响身份，路径分开就分开。
        shared = [self.root / "bots" / name / "workspace" for name in ("a", "b")]
        for path in shared:
            path.mkdir(parents=True)
            (path / "PRD.md").write_text("需求", encoding="utf-8")
        for pid, path in zip(("buddy-a", "buddy-b"), shared):
            self.ok("register_project", {"project_id": pid, "name": "workspace", "root": str(path)})
        rows = [(item["project_id"], item["root"]) for item in self.ok("list_projects")["items"]
                if item["project_id"].startswith("buddy-")]
        self.assertEqual({"buddy-a", "buddy-b"}, {pid for pid, _ in rows})
        self.assertEqual(2, len({root for _, root in rows}))

    def test_auto_registration_rolls_back_when_the_ticket_fails(self):
        # 一张单一次事务：建单没成，刚记的那一行也不该留下。
        self.error("FILE_NOT_FOUND", "create_ticket", {
            "ticket_id": "t9", "project_id": "beta", "title": "任务", "deliverable": "一份清单",
            "prd_path": "不存在的文件.md", "criteria": [{"id": "C1", "text": "条件"}],
            "root": str(self.beta),
        })
        self.assertEqual([("alpha", "甲")], self.labels())

    def test_project_id_cannot_be_pointed_at_a_second_directory(self):
        # C4 的正例：两个目录各登一次，第二次重名报 CONFLICT，第一次记录不动。
        self.ok("register_project", {"project_id": "beta", "name": "乙", "root": str(self.beta)})
        self.error("CONFLICT", "register_project", {"project_id": "alpha", "name": "甲", "root": str(self.beta)})
        self.assertEqual([("alpha", "甲"), ("beta", "乙")], self.labels())

    def test_conflicting_label_on_an_unregistered_root_is_conflict_not_reuse(self):
        # alpha 已指向项目甲；再拿 alpha 去登项目乙（未登记）不能当「重复登记」静默复用。
        self.error("CONFLICT", "register_project", {"project_id": "alpha", "name": "甲", "root": str(self.beta)})
        self.assertEqual([("alpha", "甲")], self.labels())


if __name__ == "__main__":
    unittest.main()
