"""AW-109：想不清验收条件的念头在库里有位置（PRD-014 R1 的放宽 + 池子年龄线）。

这一份用例只覆盖本单要落下的三件事，全部对着库里的行为断言，不断言提示措辞：

1. **`planned` 允许 0 条条件**：那是「已经知道要补什么、还写不出验收条件」的落点；
   `ready` 仍然必须有至少一条可判定条件，缺了报 `CRITERIA_NOT_JUDGEABLE` 且一行不写。
2. **空条件不能开工**：`claim_ticket` 读到空条件就拒，并把人送回 `revise_ticket`；
   池子视图用 `criteria_n` 把「念头」与「写好的单」分开。
3. **改自己依据的 PRD 之后能提交**（`docs/protocol.md`「改工单自己引用的 PRD 要显式
   revise_ticket」路径 1）：`revise_ticket` 不传 `restart` 时同时换掉在飞尝试上的那一份，
   于是接着提交不报 `STALE_PRD`；**不 revise 直接提交仍然报 `STALE_PRD`**——对照组留着，
   否则「放宽」会把这道闸门整个让掉。
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from task_center import Center

CRITERIA_ONE = [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}]
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "89abcdef0123456789abcdef0123456789abcdef"


class ParkingBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "念头项目"
        self.project.mkdir()
        self.prd = self.project / "PRD.md"
        self.prd.write_text("需求第一版", encoding="utf-8")
        self.db = self.root / "center.sqlite3"
        self.center = Center(self.db)
        self.counter = 0
        self.ids = {}
        self.ok("init")
        self.ok("register_project", {"project_id": "p1", "name": "念头项目", "root": str(self.project)})

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

    def payload(self, alias="t1", **extra):
        base = {"project_id": "p1", "title": f"{alias} 的念头",
                "deliverable": "一句话说清要补什么", "prd_path": "PRD.md"}
        base.update(extra)
        return base

    def create(self, alias="t1", **extra):
        created = self.ok("create_ticket", self.payload(alias, **extra))
        self.ids[alias] = created["id"]
        return created

    def tid(self, alias="t1"):
        return self.ids[alias]

    def scope(self, alias="t1", **extra):
        return {"project_id": "p1", "ticket_id": self.tid(alias), **extra}

    def detail(self, alias="t1"):
        return self.ok("get_ticket", self.scope(alias))

    def claim(self, alias="t1", **extra):
        return self.ok("claim_ticket", self.scope(
            alias, expected_revision=1, coordinator_session_id="coordinator-1", **extra))


class PlannedWithoutCriteriaTest(ParkingBase):
    """PRD-014 R1 的放宽：`planned` 可以为空，`ready` 不能。"""

    def test_planned_accepts_zero_criteria(self):
        created = self.create("park", state="planned", criteria=[])
        self.assertEqual("planned", created["state"])
        self.assertEqual([], created["criteria"])
        # 池子里看得见，且认得出来它是「念头」（criteria_n 为 0）。
        view = self.ok("list_tickets", {"project_id": "p1", "planned": True})
        self.assertIn("criteria_n", view["columns"])
        item = next(row for row in view["items"] if row["id"] == self.tid("park"))
        self.assertEqual(0, item["criteria_n"])
        # 不传 criteria 与传空数组是同一件事。
        self.assertEqual([], self.create("park2", state="planned")["criteria"])

    def test_ready_still_needs_one_criterion(self):
        """C1 的负例：ready 缺条件必须被拦，且一行不写（工单与号都不许留下）。"""
        self.error("CRITERIA_NOT_JUDGEABLE", "create_ticket",
                   self.payload("empty", state="ready", criteria=[]))
        self.error("CRITERIA_NOT_JUDGEABLE", "create_ticket", self.payload("missing", state="ready"))
        self.assertEqual([], self.ok("list_tickets")["items"])
        # 号也没被消耗：下一张单仍然是 001。
        self.assertEqual("P1-001", self.create("ok-one", criteria=CRITERIA_ONE)["id"])

    def test_shape_errors_stay_invalid_input(self):
        """放宽的是「一条都没有」，不是形状：非列表、缺 id、id 重复仍报 INVALID_INPUT。"""
        for bad in ("not-a-list", [{"id": "C1"}], [{"id": "C1", "text": ""}] * 2):
            with self.subTest(criteria=bad):
                self.error("INVALID_INPUT", "create_ticket",
                           self.payload("bad", state="planned", criteria=bad))

    def test_empty_criteria_ticket_cannot_be_claimed(self):
        """空条件的单进不了开工：claim 报 CRITERIA_NOT_JUDGEABLE 并指回 revise_ticket。"""
        self.create("park", state="planned", criteria=[])
        result = self.error("CRITERIA_NOT_JUDGEABLE", "claim_ticket", self.scope(
            "park", expected_revision=1, coordinator_session_id="coordinator-1"))
        self.assertIn("revise_ticket", result["error"]["message"])
        stopped = self.detail("park")
        self.assertEqual("planned", stopped["state"])
        self.assertEqual([], stopped["attempts"])

    def test_revise_adds_criteria_then_claim_works(self):
        """补上条件之后同一张单可以开工，判据版本 +1（条件真的变了才 +1）。"""
        self.create("park", state="planned", criteria=[])
        revised = self.ok("revise_ticket", self.scope(
            "park", expected_revision=1, reason="想清楚要验什么了", prd_path="PRD.md",
            criteria=CRITERIA_ONE))
        self.assertEqual({"from": 1, "to": 2}, revised["criteria_version"])
        self.assertEqual("planned", revised["state"])
        receipt = self.claim("park")
        self.assertEqual("dispatching", receipt["state"])
        self.assertEqual(CRITERIA_ONE, self.detail("park")["criteria"])

    def test_planned_restart_without_criteria_is_rejected(self):
        """返工 C1：`restart` 的落点是 ready，所以条件要按 ready 判。

        上一轮只按工单**当前**状态判条件，于是 `planned`（允许空）走
        `revise_ticket(restart=true, criteria=[])` 会被送回 `state=ready` 且
        `criteria=[]`：那张单谁都领不走（claim 报 CRITERIA_NOT_JUDGEABLE），
        而 docs/protocol.md 那句「`ready` 必须至少 1 项」就成了假话。
        这里同时钉两件事：操作被拒、以及库里不存在这种行。
        """
        self.create("park", state="planned", criteria=[])
        self.error("CRITERIA_NOT_JUDGEABLE", "revise_ticket", self.scope(
            "park", expected_revision=1, reason="重开一代人做", prd_path="PRD.md",
            criteria=[], restart=True))
        stopped = self.detail("park")
        self.assertEqual("planned", stopped["state"])
        self.assertEqual(1, stopped["revision"])
        self.assertEqual([], stopped["criteria"])
        # 而且不许有任何别的路留下这种行：查库断言，不是只看这张单。
        conn = sqlite3.connect(self.db)
        try:
            stray = conn.execute("SELECT id FROM tickets WHERE state='ready' AND criteria='[]'").fetchall()
        finally:
            conn.close()
        self.assertEqual([], stray)
        # 补上条件之后 restart 照旧可用：拦的是空条件，不是 restart。
        revised = self.ok("revise_ticket", self.scope(
            "park", expected_revision=1, reason="条件想清楚了", prd_path="PRD.md",
            criteria=CRITERIA_ONE, restart=True))
        self.assertEqual("ready", revised["state"])
        self.assertEqual(CRITERIA_ONE, revised["criteria"])

    def test_reopen_delivered_without_criteria_is_rejected(self):
        """同一条不变量：`reopen` 的落点也是 ready。"""
        self.create("t1", criteria=CRITERIA_ONE)
        attempt = self.claim("t1")["attempt_id"]
        self.ok("bind_session", self.scope("t1", attempt_id=attempt, session_id="executor-1",
                                          project_root=str(self.project)))
        (self.project / "结果.md").write_text("成果", encoding="utf-8")
        submitted = self.ok("submit_result", self.scope(
            "t1", attempt_id=attempt, session_id="executor-1", summary="成果已完成",
            artifacts=["结果.md"], evidence=["跑过一遍"], risks=[],
            head_sha=HEAD_SHA, base_sha=BASE_SHA))
        self.ok("review_result", self.scope(
            "t1", attempt_id=attempt, submission_id=submitted["submission_id"],
            reviewer_session_id="reviewer-1", verdict="accept",
            checks=[{"id": "C1", "passed": True, "evidence": "跑过且退出码 0"}],
            reason="通过"))
        # done → reopen 的落点是 ready，空条件同样不许留下。
        self.error("CRITERIA_NOT_JUDGEABLE", "revise_ticket", self.scope(
            "t1", expected_revision=1, reason="重开", prd_path="PRD.md",
            criteria=[], reopen=True))
        self.assertEqual("done", self.detail("t1")["state"])

    def test_planned_revise_without_criteria_keeps_it_empty(self):
        """池子里的单可以反复改描述而条件照旧空着；但 ready 的单不许被改成空条件。"""
        self.create("park", state="planned", criteria=[])
        revised = self.ok("revise_ticket", self.scope(
            "park", expected_revision=1, reason="把主题写细一点", prd_path="PRD.md",
            deliverable="补一句话：这条要补的是池子年龄线", criteria=[]))
        self.assertEqual([], revised["criteria"])
        self.assertEqual({"from": 1, "to": 1}, revised["criteria_version"])
        self.create("ready-one", criteria=CRITERIA_ONE)
        self.error("CRITERIA_NOT_JUDGEABLE", "revise_ticket", self.scope(
            "ready-one", expected_revision=1, reason="清空条件", prd_path="PRD.md", criteria=[]))


class ReviseRefreezesPrdTest(ParkingBase):
    """改自己依据的 PRD：显式 revise 之后能提交，不 revise 直接提交仍然被拦。"""

    def ready_ticket_under_edit(self, alias="t1"):
        attempt = self.claim(alias)["attempt_id"]
        session = f"executor-{alias}"
        self.ok("bind_session", self.scope(alias, attempt_id=attempt, session_id=session,
                                          project_root=str(self.project)))
        self.prd.write_text("需求第一版，加一条：池子里的单超过 7 天要报出来", encoding="utf-8")
        return attempt, session

    def submit(self, alias, attempt, session, name="结果.md"):
        (self.project / name).write_text("成果", encoding="utf-8")
        return self.call("submit_result", self.scope(
            alias, attempt_id=attempt, session_id=session, summary="成果已完成",
            artifacts=[name], evidence=["跑过一遍"], risks=[], head_sha=HEAD_SHA, base_sha=BASE_SHA))

    def test_prd_edit_without_revise_still_stale(self):
        """对照组：谁都别想「改了 PRD 不动工单就提交」。"""
        self.create("t1", criteria=CRITERIA_ONE)
        attempt, session = self.ready_ticket_under_edit("t1")
        result = self.submit("t1", attempt, session)
        self.assertFalse(result["ok"], result)
        self.assertEqual("STALE_PRD", result["error"]["code"], result)

    def test_revise_after_prd_edit_lets_the_attempt_submit(self):
        """路径 1：先改文件、再 revise_ticket（不 restart），旧尝试照旧在飞并接着提交。"""
        self.create("t1", criteria=CRITERIA_ONE)
        attempt, session = self.ready_ticket_under_edit("t1")
        revised = self.ok("revise_ticket", self.scope(
            "t1", expected_revision=1, reason="本单要改的就是这份 PRD，先重取摘要",
            prd_path="PRD.md", criteria=CRITERIA_ONE))
        self.assertEqual("running", revised["state"])
        self.assertIsNotNone(revised["prd_sha256"])
        # 尝试行上的那一份也换掉了：STALE_PRD 比的是它。
        stored = self.ok("get_ticket", self.scope("t1"))
        current = next(row for row in stored["attempts"] if row["id"] == attempt)
        self.assertEqual(revised["prd_sha256"], current["prd_sha256"])
        result = self.submit("t1", attempt, session)
        self.assertTrue(result["ok"], result)
        self.assertEqual("awaiting_review", self.detail("t1")["state"])


if __name__ == "__main__":
    unittest.main()
