"""PRD-014 R1/R2/R3/R5/R6：schema 11 的计划队列——可建、可列、可领、可等。

这一份用例只覆盖本单（AW-097）要落下的四件事：

1. **发号**（R5）：号在 `create_ticket` 那一刻由库发，调用方指定即拒；每个项目一条
   独立序列，展示形态 `<前缀>-<序号>`。
2. **指纹挪到 claim**（R2）：`create_ticket` 只记 `prd_path`，`tickets.prd_sha256`
   在排队期间是 NULL；`claim_ticket` 自己取 sha256 写进尝试并回写工单。排队期间 PRD
   升版不让单过期，claim 之后再升版仍旧 `STALE_PRD`。
3. **依赖边**（R3）：`blocked_by` 未 done 时 `claim_ticket` 报 `BLOCKED_BY_DEPENDENCY`
   并点名前置单与它当前的状态；成环报 `DEPENDENCY_CYCLE`。
4. **规划期可见性**（R6）：`list_tickets --planned` 按依赖层数升序列出池子，每张带
   `claimable` 与 `blocked_by`。`claimable` 只表示「依赖都到位」，不表示「现在就能领」
   ——同项目已有在途单时 `claim_ticket` 仍报 `DISPATCH_BUSY`，那是另一件事（AC-R6-3）。
"""
import hashlib
import tempfile
import unittest
from pathlib import Path

from task_center import Center

CRITERIA_V1 = [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}]
CRITERIA_V2 = [{"id": "C1", "text": "含 `git status --short` 且退出码 0"}]

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "89abcdef0123456789abcdef0123456789abcdef"


class PlannedQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "计划项目"
        self.project.mkdir()
        (self.project / "PRD.md").write_text("需求第一版", encoding="utf-8")
        self.other = self.root / "另一个项目"
        self.other.mkdir()
        (self.other / "PRD.md").write_text("另一份需求", encoding="utf-8")
        self.db = self.root / "center.sqlite3"
        self.center = Center(self.db)
        self.counter = 0
        self.ids = {}
        self.ok("init")
        self.ok("register_project", {"project_id": "p1", "name": "计划项目", "root": str(self.project)})
        self.ok("register_project", {"project_id": "p2", "name": "另一个项目", "root": str(self.other)})

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

    def create(self, alias="t1", project_id="p1", state=None, prd_path="PRD.md", **extra):
        """建一张单，把库发的号记在 self.ids[alias] 下（号不再是测试说了算）。"""
        payload = {"project_id": project_id, "title": f"{alias} 的检查清单",
                   "deliverable": "一份逐项可勾选的检查清单",
                   "prd_path": prd_path, "criteria": CRITERIA_V1, **extra}
        if state is not None:
            payload["state"] = state
        created = self.ok("create_ticket", payload)
        self.ids[alias] = created["id"]
        return created

    def tid(self, alias):
        return self.ids[alias]

    def scope(self, alias="t1", **extra):
        return {"project_id": "p1", "ticket_id": self.tid(alias), **extra}

    def claim(self, alias="t1", **extra):
        return self.ok("claim_ticket", self.scope(
            alias, expected_revision=1, coordinator_session_id="coordinator-1", **extra))

    def finish(self, alias="t1"):
        """把一张单走到 done：claim → bind → submit → review(accept)。"""
        attempt = self.claim(alias)["attempt_id"]
        session = f"executor-{alias}"
        self.ok("bind_session", self.scope(alias, attempt_id=attempt, session_id=session,
                                          project_root=str(self.project)))
        (self.project / f"{alias}.md").write_text("1. 检查步骤", encoding="utf-8")
        submission = self.ok("submit_result", self.scope(
            alias, attempt_id=attempt, session_id=session, summary="清单已完成",
            artifacts=[f"{alias}.md"], evidence=["人工检查条目"], risks=[],
            head_sha=HEAD_SHA, base_sha=BASE_SHA))
        return self.ok("review_result", self.scope(
            alias, attempt_id=attempt, submission_id=submission["submission_id"],
            reviewer_session_id="reviewer-1", verdict="accept",
            checks=[{"id": "C1", "passed": True, "evidence": "逐项核过"}], reason="逐项核对"))

    def detail(self, alias="t1"):
        return self.ok("get_ticket", self.scope(alias))

    # --- R5 库里发号 -------------------------------------------------------

    def test_create_ticket_assigns_number(self):
        """号由库发：回执里的号就是主键，前缀取项目短名，create 不取 PRD 指纹。"""
        created = self.create("t1")
        self.assertEqual("P1-001", created["id"])
        self.assertEqual("ready", created["state"])
        # R2.1：排队期间不取指纹，create 只记 prd_path。
        self.assertIsNone(created["prd_sha256"])
        self.assertEqual("PRD.md", created["prd_path"])
        # 回执里的号就是查询用的那个号：不是自报值，也不是另起的别名。
        self.assertEqual("P1-001", self.detail("t1")["id"])
        second = self.create("t2")
        self.assertEqual("P1-002", second["id"])

    def test_create_ticket_rejects_caller_id(self):
        """调用方给 ticket_id 一律拒：号由库里发，回执里的才是真号。"""
        result = self.call("create_ticket", {
            "ticket_id": "AW-099", "project_id": "p1", "title": "自报编号",
            "deliverable": "一份清单", "prd_path": "PRD.md", "criteria": CRITERIA_V1,
        })
        self.assertFalse(result["ok"], result)
        self.assertEqual("INVALID_INPUT", result["error"]["code"], result)
        self.assertIn("号由库里发", result["error"]["message"])
        # 被拒时一行不写：池子里没有这张单，号也没有被消耗掉。
        self.assertEqual([], self.ok("list_tickets")["items"])
        self.assertEqual("P1-001", self.create("t1")["id"])

    def test_create_ticket_records_its_number_on_the_event(self):
        """建单那条事件挂在库发的那张单上，不是 NULL——否则「这号什么时候建的」按单查不到。"""
        created = self.create("t1")
        events = self.ok("events", self.scope("t1"))["items"]
        self.assertEqual(["create_ticket"], [item["operation"] for item in events])
        self.assertEqual(created["id"], self.detail("t1")["id"])

    def test_number_sequence_per_project(self):
        """跨项目不共用一条序列：p1 与 p2 各自从 1 开始。"""
        self.assertEqual("P1-001", self.create("a1", project_id="p1")["id"])
        self.assertEqual("P2-001", self.create("b1", project_id="p2")["id"])
        self.assertEqual("P1-002", self.create("a2", project_id="p1")["id"])
        self.assertEqual("P2-002", self.create("b2", project_id="p2")["id"])

    # --- R2 指纹从 create 挪到 claim ---------------------------------------

    def test_planned_ticket_survives_prd_bump_before_claim(self):
        """排队期间 PRD 升版不算过期：依据在 claim 那一刻才定格。"""
        self.create("t1", state="planned")
        planned = self.detail("t1")
        self.assertEqual("planned", planned["state"])
        self.assertIsNone(planned["prd_sha256"])
        self.assertEqual([], planned["attempts"])
        (self.project / "PRD.md").write_text("需求第二版：改了验收尺寸", encoding="utf-8")
        bumped = hashlib.sha256((self.project / "PRD.md").read_bytes()).hexdigest()
        claimed = self.claim("t1")
        self.assertEqual("dispatching", claimed["state"])
        after = self.detail("t1")
        self.assertEqual(bumped, after["prd_sha256"])
        self.assertEqual(bumped, after["attempts"][0]["prd_sha256"])
        self.assertEqual("PRD.md", after["attempts"][0]["prd_path"])

    def test_prd_bump_after_claim_still_stale(self):
        """定格之后照样算数：claim 取了指纹，PRD 再变就是 STALE_PRD。"""
        self.create("t1")
        attempt = self.claim("t1")["attempt_id"]
        self.ok("bind_session", self.scope(attempt_id=attempt, session_id="executor-1",
                                          project_root=str(self.project)))
        (self.project / "PRD.md").write_text("需求第二版", encoding="utf-8")
        (self.project / "result.md").write_text("1. 检查步骤", encoding="utf-8")
        self.error("STALE_PRD", "submit_result", self.scope(
            attempt_id=attempt, session_id="executor-1", summary="清单已完成",
            artifacts=["result.md"], evidence=["人工检查条目"], risks=[],
            head_sha=HEAD_SHA, base_sha=BASE_SHA))

    # --- R3 依赖三类 -------------------------------------------------------

    def test_claim_blocked_by_pending_names_the_ticket(self):
        """前置没过就领不动，message 里点名是哪张单、它当前在什么状态。"""
        self.create("blocker", state="planned")
        self.create("dep", state="planned", blocked_by=[self.tid("blocker")])
        result = self.call("claim_ticket", self.scope(
            "dep", expected_revision=1, coordinator_session_id="coordinator-1"))
        self.assertFalse(result["ok"], result)
        self.assertEqual("BLOCKED_BY_DEPENDENCY", result["error"]["code"], result)
        message = result["error"]["message"]
        self.assertIn(self.tid("blocker"), message)
        self.assertIn("planned", message)
        # 一行不写：尝试没有产生，单还停在 planned，可以等前置做完再领。
        self.assertEqual([], self.detail("dep")["attempts"])
        self.assertEqual("planned", self.detail("dep")["state"])

    def test_claim_blocked_by_done_ok(self):
        """前置 done 之后下游才能领；cancelled 不算完成（另有用例）。"""
        self.create("blocker")
        self.create("dep", state="planned", blocked_by=[self.tid("blocker")])
        self.finish("blocker")
        self.assertEqual("done", self.detail("blocker")["state"])
        claimed = self.claim("dep")
        self.assertEqual("dispatching", claimed["state"])

    def test_claim_blocked_by_cancelled_is_not_done(self):
        """取消不是完成：cancelled 的前置仍算未满足，下游不自动作废也不放行。"""
        self.create("blocker")
        self.create("dep", state="planned", blocked_by=[self.tid("blocker")])
        self.ok("cancel_ticket", self.scope("blocker", expected_revision=1, reason="不做了"))
        result = self.call("claim_ticket", self.scope(
            "dep", expected_revision=1, coordinator_session_id="coordinator-1"))
        self.assertEqual("BLOCKED_BY_DEPENDENCY", result["error"]["code"], result)
        self.assertIn("cancelled", result["error"]["message"])
        self.assertEqual("planned", self.detail("dep")["state"])

    def test_dependency_cycle_rejected(self):
        """写边那一刻查一次可达性，成环报 DEPENDENCY_CYCLE 并点名环上的单。"""
        self.create("a", state="planned")
        self.create("b", state="planned", blocked_by=[self.tid("a")])
        result = self.call("revise_ticket", self.scope(
            "a", expected_revision=1, reason="补依赖", prd_path="PRD.md",
            criteria=CRITERIA_V1, blocked_by=[self.tid("b")]))
        self.assertFalse(result["ok"], result)
        self.assertEqual("DEPENDENCY_CYCLE", result["error"]["code"], result)
        message = result["error"]["message"]
        self.assertIn(self.tid("a"), message)
        self.assertIn(self.tid("b"), message)
        # 一行不写：环没有被写进去，A 的出边仍然是空。
        self.assertEqual([], self.detail("a")["links"]["out"])

    # --- R6 规划期可见性 ---------------------------------------------------

    def test_planned_view_lists_by_dependency_order_with_claimable(self):
        """池子按依赖层数升序：无前置的先出现，同一层按建单时间。"""
        self.create("b", state="planned")
        self.create("a", state="planned", blocked_by=[self.tid("b")])
        view = self.ok("list_tickets", {"planned": True})
        self.assertIn("claimable", view["columns"])
        self.assertIn("blocked_by", view["columns"])
        self.assertEqual([self.tid("b"), self.tid("a")], [item["id"] for item in view["items"]])
        first, second = view["items"]
        self.assertEqual([], first["blocked_by"])
        self.assertTrue(first["claimable"])
        self.assertEqual([self.tid("b")], second["blocked_by"])
        self.assertFalse(second["claimable"])
        # 依赖到位之后 claimable 才变 true；B 走完整条链到 done。
        self.finish("b")
        after = {item["id"]: item for item in self.ok("list_tickets", {"planned": True})["items"]}
        self.assertTrue(after[self.tid("a")]["claimable"])
        self.assertEqual([], after[self.tid("a")]["blocked_by"])
        # 默认视图不多这两列（PRD-014 R6.2：只在 planned 视图出现）。
        plain = self.ok("list_tickets")["columns"]
        self.assertNotIn("claimable", plain)
        self.assertNotIn("blocked_by", plain)

    def test_planned_flag_accepts_the_cli_spelling(self):
        """CLI 糖 `--planned` 的两种形态等价：`planned` 与 `--planned` 同名一义。"""
        self.create("b", state="planned")
        by_alias = self.ok("list_tickets", {"--planned": True})
        by_name = self.ok("list_tickets", {"planned": True})
        self.assertEqual([item["id"] for item in by_name["items"]],
                         [item["id"] for item in by_alias["items"]])
        self.assertEqual(by_name["columns"], by_alias["columns"])
        self.error("INVALID_INPUT", "list_tickets", {"planned": "true"})

    def test_planned_ticket_does_not_block_another_claim(self):
        """池子里的单不占「在途」：planned 在场时另一张单照样领得动。"""
        self.create("waiting", state="planned")
        self.create("now")
        claimed = self.claim("now")
        self.assertEqual("dispatching", claimed["state"])

    def test_claimable_is_not_the_same_as_can_claim_now(self):
        """AC-R6-3：claimable 只说依赖到位，不替 DISPATCH_BUSY 回答问题。"""
        self.create("blocker")
        self.create("dep", state="planned", blocked_by=[self.tid("blocker")])
        self.finish("blocker")
        # blocker 已经 done，池子里 A 的依赖到位。
        view = {item["id"]: item for item in self.ok("list_tickets", {"planned": True})["items"]}
        self.assertTrue(view[self.tid("dep")]["claimable"])
        # 但同项目现在有一张在途单（blk 已 done，所以用另一张在跑来造现场）。
        self.create("busy")
        self.claim("busy")
        busy_error = self.call("claim_ticket", self.scope(
            "dep", expected_revision=1, coordinator_session_id="coordinator-1"))
        self.assertEqual("DISPATCH_BUSY", busy_error["error"]["code"], busy_error)

    def test_planned_ticket_is_cancellable_and_has_no_attempt(self):
        """planned 单不进尝试：不占 active_session 槽位，取消也不产生尝试。"""
        self.create("t1", state="planned")
        self.assertEqual([], self.detail("t1")["attempts"])
        self.error("STALE_ATTEMPT", "bind_session", self.scope(
            "t1", attempt_id="0" * 8, session_id="executor-1", project_root=str(self.project)))
        cancelled = self.ok("cancel_ticket", self.scope("t1", expected_revision=1, reason="不排了"))
        self.assertEqual("cancelled", cancelled["state"])
        self.assertEqual([], self.detail("t1")["attempts"])

    def test_get_ticket_reports_links_in_both_directions(self):
        """依赖边两头都答得出：A 的出边指向 B，B 的入边指向 A。"""
        self.create("b", state="planned")
        self.create("a", state="planned", blocked_by=[self.tid("b")], related=[])
        detail = self.detail("a")
        self.assertEqual([{"kind": "blocked_by", "ticket_id": self.tid("b"), "state": "planned"}],
                         detail["links"]["out"])
        self.assertEqual([], detail["links"]["in"])
        reverse = self.detail("b")
        self.assertEqual([], reverse["links"]["out"])
        self.assertEqual([{"kind": "blocked_by", "ticket_id": self.tid("a"), "state": "planned"}],
                         reverse["links"]["in"])

    def test_prd_sha256_is_only_written_at_claim_and_revise(self):
        """R2 的反例面：没有第三条路径会给没领过的单带上指纹。"""
        self.create("t1", state="planned")
        self.assertIsNone(self.detail("t1")["prd_sha256"])
        self.ok("revise_ticket", self.scope(
            "t1", expected_revision=1, reason="补一条验收条件", prd_path="PRD.md",
            criteria=CRITERIA_V2))
        # planned 单改路径/判据仍留 NULL：排队期间不取指纹这一条不因为 revise 而失效。
        self.assertIsNone(self.detail("t1")["prd_sha256"])
        self.claim("t1")
        self.assertEqual(64, len(self.detail("t1")["prd_sha256"]))


if __name__ == "__main__":
    unittest.main()
