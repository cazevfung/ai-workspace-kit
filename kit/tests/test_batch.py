"""schema 12：车次——一个窗口里持多张单（PRD-014 R4）。

这一份只验本单落下的四件事：

1. **同一车次内一个会话持多张单**：车次内两张单各是一条活跃尝试，可以同时 `running`
   ——靠的是 `active_session` 收窄成 `WHERE batch_key IS NULL …`，不是靠放宽会话维度。
2. **没有车次的单行为一字不变**：同一会话的第二张无车次单仍旧 `CONFLICT`。
3. **车次归属只有一个判据**：`batches.owner_session_id` + `claim_ticket` 的运行期校验。
   另一个会话领同一车次报 `BATCH_OWNED_BY_OTHER`，且一个尝试、一条工单改动都不留
   （只留一行拒收事件）。索引不表达归属，`attempts` 上也没有车次维度的唯一键。
4. **释放时机各靠什么失效**：单子终结时尝试离开 `blocked`/`running`/`submitted`，
   索引条件失效、槽位自动空出；车次本身不因单子终结而关闭（`closed_at` 仍为空），
   关闭由最后一张单的合并记录那一侧落，`closed_at` 一旦非空，车次不再接受新单。

车次行目前没有建车次的操作（`PRD-015` 的搭车实施单才做），所以夹具直接写库：
插一行 `batches`、把 `tickets.batch_id` 指过去。这与「本单只落 schema 与 claim 闸门」
同一范围，不是绕开被测代码——被验的是 claim / bind / 索引。
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from task_center import Center

CRITERIA = [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}]
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "89abcdef0123456789abcdef0123456789abcdef"


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "车次项目"
        self.project.mkdir()
        (self.project / "PRD.md").write_text("需求第一版", encoding="utf-8")
        self.db = self.root / "center.sqlite3"
        self.center = Center(self.db)
        self.counter = 0
        self.ids = {}
        self.ok("init")
        self.ok("register_project", {"project_id": "p1", "name": "车次项目", "root": str(self.project)})

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

    def ticket(self, alias):
        created = self.ok("create_ticket", {
            "project_id": "p1", "title": f"{alias} 的检查清单",
            "deliverable": "一份逐项可勾选的检查清单",
            "prd_path": "PRD.md", "criteria": CRITERIA})
        self.ids[alias] = created["id"]
        return created["id"]

    def tid(self, alias):
        return self.ids[alias]

    def scope(self, alias, **extra):
        return {"project_id": "p1", "ticket_id": self.tid(alias), **extra}

    def batch(self, batch_id="b1", resource_key="资源:仓库:示例项目", closed_at=None):
        """写一行车次（没有建车次的操作，见文件头）。"""
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("INSERT INTO batches(id,project_id,resource_key,owner_session_id,planned_at,closed_at)"
                         " VALUES (?,?,?,NULL,?,?)",
                         (batch_id, "p1", resource_key, "2026-11-21T12:00:00+00:00", closed_at))
            conn.commit()
        finally:
            conn.close()

    def ride(self, alias, batch_id="b1"):
        """把一张单放进车次（同上：搭车属于 PRD-015 的实施单）。"""
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE tickets SET batch_id=? WHERE id=?", (batch_id, self.tid(alias)))
            conn.commit()
        finally:
            conn.close()

    def claim(self, alias, coordinator="coordinator-1", **extra):
        return self.ok("claim_ticket", self.scope(
            alias, expected_revision=1, coordinator_session_id=coordinator, **extra))

    def bind(self, attempt, alias, session="executor-1"):
        return self.ok("bind_session", self.scope(
            alias, attempt_id=attempt, session_id=session, project_root=str(self.project)))

    def detail(self, alias):
        return self.ok("get_ticket", self.scope(alias))

    def batch_row(self, batch_id="b1"):
        conn = sqlite3.connect(self.db)
        try:
            conn.row_factory = sqlite3.Row
            return dict(conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone())
        finally:
            conn.close()

    def attempt_count(self, alias):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM attempts WHERE ticket_id=?", (self.tid(alias),)).fetchone()[0]
        finally:
            conn.close()

    def index_sql(self, name="active_session"):
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def finish(self, attempt, alias, session="executor-1"):
        """把一张单推到 done：提交 + 验收通过。"""
        result = self.project / f"result-{alias}.md"
        result.write_text("1. 检查步骤", encoding="utf-8")
        self.ok("submit_result", self.scope(
            alias, attempt_id=attempt, session_id=session, artifacts=[result.name],
            summary="清单已完成", evidence=["人工检查条目"], risks=[],
            head_sha=HEAD_SHA, base_sha=BASE_SHA))
        submission = self.detail(alias)["submissions"][-1]
        return self.ok("review_result", self.scope(
            alias, attempt_id=attempt, submission_id=submission["id"],
            reviewer_session_id="reviewer-1", verdict="accept",
            checks=[{"id": "C1", "passed": True, "evidence": "逐条核对"}], reason="条件都过"))

    # --- 1. 车次内一个会话持多张单 -----------------------------------------

    def test_two_tickets_in_one_batch_both_active(self):
        """AC-R4-1：同一会话、同一车次的两张单**同时**是 running。"""
        self.ticket("t1")
        self.ticket("t2")
        self.batch()
        self.ride("t1")
        self.ride("t2")
        # 车次第一次被领：持有会话写进来，回执里看得见。
        first = self.claim("t1")
        self.assertEqual("b1", first["batch_id"])
        self.assertEqual("coordinator-1", first["batch_owner_session_id"])
        self.assertEqual("coordinator-1", self.batch_row()["owner_session_id"])
        self.bind(first["attempt_id"], "t1")
        # 第二张同车次的单：同一个会话接着领（同项目有在途单，所以要显式放过并行）。
        second = self.claim("t2", allow_parallel=True, parallel_reason="同一车次 b1 内的两张单共用一次构建")
        self.assertEqual("b1", second["batch_id"])
        self.bind(second["attempt_id"], "t2")
        # 两张单同时 running，同一条会话，同一个 batch_key。
        for alias in ("t1", "t2"):
            detail = self.detail(alias)
            self.assertEqual("running", detail["state"])
            self.assertEqual("executor-1", detail["attempts"][-1]["session_id"])
            self.assertEqual("b1", detail["attempts"][-1]["batch_key"])
            self.assertEqual("b1", detail["batch_id"])
        # 索引只守无车次那一半：有车次的尝试压根不在它的范围内。
        self.assertIn("WHERE batch_key IS NULL", self.index_sql())
        conn = sqlite3.connect(self.db)
        try:
            held = conn.execute("SELECT COUNT(*) FROM attempts WHERE session_id='executor-1' AND state='running'"
                                ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(2, held, "同一会话在同一车次里应当同时持有两条活跃尝试")

    # --- 2. 无车次的单行为不变 ---------------------------------------------

    def test_same_session_second_ticket_outside_batch_rejected(self):
        """AC-R4-1 对照组：没有车次的第二张单，同一会话仍被 CONFLICT 拒。"""
        self.ticket("t1")
        self.ticket("t2")
        self.bind(self.claim("t1")["attempt_id"], "t1")
        attempt = self.claim("t2", allow_parallel=True, parallel_reason="两个互不触碰的清单")["attempt_id"]
        self.error("CONFLICT", "bind_session", self.scope(
            "t2", attempt_id=attempt, session_id="executor-1", project_root=str(self.project)))
        # 被拒之后状态一个都不许动：t1 还在跑，t2 还停在 dispatching。
        self.assertEqual("running", self.detail("t1")["state"])
        self.assertEqual("dispatching", self.detail("t2")["state"])
        self.assertIsNone(self.detail("t2")["attempts"][-1]["batch_key"])

    # --- 3. 车次归属只有一个判据 -------------------------------------------

    def test_batch_owned_by_other_rejected(self):
        """AC-R4-1 第二条：另一个会话领同一车次报 BATCH_OWNED_BY_OTHER，且不留行。"""
        self.ticket("t1")
        self.ticket("t2")
        self.batch()
        self.ride("t1")
        self.ride("t2")
        self.bind(self.claim("t1")["attempt_id"], "t1")
        before = self.detail("t2")
        result = self.error("BATCH_OWNED_BY_OTHER", "claim_ticket", self.scope(
            "t2", expected_revision=1, coordinator_session_id="coordinator-2",
            allow_parallel=True, parallel_reason="另一位协调者想接同一车次"))
        self.assertIn("b1", result["error"]["message"])
        self.assertIn("coordinator-1", result["error"]["message"])
        # 一个尝试都不产生，工单行原样不动，车次持有者也不变。
        self.assertEqual(0, self.attempt_count("t2"))
        after = self.detail("t2")
        self.assertEqual(before["state"], after["state"])
        self.assertEqual(before["revision"], after["revision"])
        self.assertIsNone(after["current_attempt"])
        self.assertEqual("coordinator-1", self.batch_row()["owner_session_id"])
        # 归属不是索引：`attempts` 上只有那一条收窄过的 `active_session`，
        # 没有车次维度的唯一索引，也没有把车次写成唯一列。
        conn = sqlite3.connect(self.db)
        try:
            indexes = {row[1]: bool(row[2]) for row in conn.execute("PRAGMA index_list(attempts)")
                       if not row[1].startswith("sqlite_autoindex")}
        finally:
            conn.close()
        self.assertEqual({"active_session": True}, indexes)

    # --- 4. 释放时机 -------------------------------------------------------

    def test_batch_release_on_ticket_done(self):
        """单子终结释放的是尝试槽位，不是车次（PRD-014 R4.4 第二行）。"""
        self.ticket("t1")
        self.ticket("t2")
        self.ticket("t3")
        self.batch()
        for alias in ("t1", "t2", "t3"):
            self.ride(alias)
        first = self.claim("t1")["attempt_id"]
        self.bind(first, "t1")
        second = self.claim("t2", allow_parallel=True, parallel_reason="同一车次内接着做的第二张")["attempt_id"]
        self.bind(second, "t2")
        self.assertEqual("running", self.detail("t1")["state"])
        self.finish(first, "t1")
        done = self.detail("t1")
        self.assertEqual("done", done["state"])
        # 尝试离开 blocked/running/submitted：索引条件失效，槽位自动空出（不是一条清理动作）。
        self.assertEqual("accepted", done["attempts"][-1]["state"])
        # 车次不因某一张单终结而关闭：closed_at 仍为空，持有会话照旧是它。
        row = self.batch_row()
        self.assertIsNone(row["closed_at"])
        self.assertEqual("coordinator-1", row["owner_session_id"])
        # 同一个持有者可以在这个车次里接着领第三张，并仍旧交给同一个执行会话。
        third = self.claim("t3", allow_parallel=True, parallel_reason="同一车次内接着做的第三张")["attempt_id"]
        self.bind(third, "t3")
        self.assertEqual("running", self.detail("t3")["state"])
        self.assertEqual("running", self.detail("t2")["state"])

    def test_closed_batch_rejects_new_claim(self):
        """`closed_at` 之后车次不再接受新单——关闭靠落这一列，不靠一个状态字段。"""
        self.ticket("t1")
        self.batch(closed_at="2026-11-21T13:00:00+00:00")
        self.ride("t1")
        closed = "2026-11-21T13:00:00+00:00"
        result = self.error("INVALID_STATE", "claim_ticket", self.scope(
            "t1", expected_revision=1, coordinator_session_id="coordinator-1"))
        self.assertIn("b1", result["error"]["message"])
        self.assertIn(closed, result["error"]["message"])
        self.assertEqual(0, self.attempt_count("t1"))
        self.assertEqual("ready", self.detail("t1")["state"])
        self.assertIsNone(self.batch_row()["owner_session_id"], "被拒的调用不许顺手把车次认走")


if __name__ == "__main__":
    unittest.main()
