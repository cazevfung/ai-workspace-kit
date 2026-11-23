"""`tools/handover_owner.py` 的用例：接替只在「原来的责任人已经不在了」时发生。

AW-122 选定的那条路是：无人值守时回执的收件人是 `tickets.owner_session_id`，派单器
`claim` 之后把 owner 显式接替到一条活着的常驻会话。这条命令就是那一步，判据只有两类：

- **该接的接得成**：副本库上造「协调会话已归档」的现场，`claim` → 接替 → `get_ticket`
  的 `owner_session_id` 指向一条活会话，新 owner 的 `record_notice` 能落库（C3 的现场）。
- **不该接的一条也不许过**：owner 还活着（那是「把消息发回它自己」的场景，不是接替）、
  目标会话已经归档、这张单已经有合并记录或回执（收尾只剩合并，而合并不认会话）、
  目标是执行会话自己、工单已取消、空理由、宿主会话库读不出来（「不知道」不许长得像
  「可以接」）。每条都要**一行不写**。

跑法：

    python tools/test_handover_owner.py      # 单独跑，用例名逐条打印
    python tools/run_tests.py                # 随主线套件跑
"""
import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
if str(TOOLS.parent) not in sys.path:
    sys.path.insert(0, str(TOOLS.parent))

import handover_owner  # noqa: E402
from task_center import Center  # noqa: E402

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "89abcdef0123456789abcdef0123456789abcdef"

ARCHIVED = "coordinator-1"   # 派单器：claim 之后被宿主归档
RESIDENT = "coordinator-2"   # 常驻（值班）会话：接替的目标
EXECUTOR = "executor-1"
STAMP_MS = 1795334648266


def build_sessions(path, rows, reset=False):
    """宿主会话库只要 `sessions` 这几列：与 `tools/coordinator_state.py` 读的是同一份。"""
    conn = sqlite3.connect(path)
    try:
        if reset:
            conn.execute("DROP TABLE IF EXISTS sessions")
        conn.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT, working_dir TEXT,"
                     " status TEXT, source TEXT, created_at INTEGER, updated_at INTEGER,"
                     " last_turn_ended_at INTEGER, hidden_at TEXT)")
        for row in rows:
            conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)", row)
        conn.commit()
    finally:
        conn.close()
    return path


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "项目一"
        self.project.mkdir()
        (self.project / "PRD.md").write_text("需求第一版", encoding="utf-8")
        self.db = self.root / "center.sqlite3"
        self.sessions_db = build_sessions(self.root / "cindy-fake.db", [
            (ARCHIVED, "AI工作区无人值守派单器", "{{WORKSPACE}}", "archived", "schedule",
             STAMP_MS, STAMP_MS, STAMP_MS, None),
            (RESIDENT, "AI工作区值班", "{{WORKSPACE}}", "active", "user",
             STAMP_MS, STAMP_MS, STAMP_MS, None),
            ("coordinator-3", "已归档的另一条", "{{WORKSPACE}}", "archived", "user",
             STAMP_MS, STAMP_MS, STAMP_MS, None),
        ])
        self.center = Center(self.db)
        self.counter = 0
        self.ids = {}
        self.ok("init")
        self.ok("register_project", {"project_id": "p1", "name": "项目一", "root": str(self.project)})

    # --- 夹具 -------------------------------------------------------------

    def call(self, op, payload=None):
        self.counter += 1
        payload = payload or {}
        alias = payload.get("ticket_id")
        if isinstance(alias, str) and alias in self.ids:
            payload = {**payload, "ticket_id": self.ids[alias]}
        return self.center.execute(op, payload, f"req-{self.counter}")

    def ok(self, op, payload=None):
        result = self.call(op, payload)
        self.assertTrue(result["ok"], result)
        return result["result"]

    def error(self, code, op, payload=None):
        result = self.call(op, payload)
        self.assertFalse(result["ok"], result)
        self.assertEqual(code, result["error"]["code"], result)
        return result

    def scope(self, **extra):
        return {"project_id": "p1", "ticket_id": "t1", **extra}

    def ticket(self):
        created = self.ok("create_ticket", {
            "project_id": "p1", "title": "整理检查清单",
            "deliverable": "一份逐项可勾选的检查清单",
            "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        })
        self.ids["t1"] = created["id"]
        return created

    def dispatched(self, coordinator=ARCHIVED):
        """派单器 claim + 执行会话 bind：这就是无人值守那条链的第一步。"""
        self.ticket()
        claim = self.ok("claim_ticket", self.scope(expected_revision=1,
                                                  coordinator_session_id=coordinator))
        self.ok("bind_session", self.scope(attempt_id=claim["attempt_id"], session_id=EXECUTOR,
                                          project_root=str(self.project)))
        return claim["attempt_id"]

    def delivered(self):
        """一路做到 done 且回执已记：收尾只剩合并。"""
        attempt = self.dispatched()
        (self.project / "result.md").write_text("1. 检查步骤", encoding="utf-8")
        submission = self.ok("submit_result", {
            **self.scope(), "attempt_id": attempt, "session_id": EXECUTOR,
            "artifacts": ["result.md"], "summary": "清单已完成", "evidence": ["人工检查条目"],
            "risks": [], "head_sha": HEAD_SHA, "base_sha": BASE_SHA,
        })
        self.ok("review_result", {
            **self.scope(), "attempt_id": attempt, "submission_id": submission["submission_id"],
            "reviewer_session_id": "reviewer-1", "verdict": "accept",
            "checks": [{"id": "C1", "passed": True, "evidence": "已阅读清单并核对步骤"}],
            "reason": "逐项检查后作出结论",
        })
        return attempt

    def run_tool(self, *extra, to=RESIDENT, reason="派单器 claim 之后被宿主归档，接替到值班会话"):
        argv = ["--ticket", self.ids.get("t1", "t1"), "--db", str(self.db),
                "--sessions-db", str(self.sessions_db)]
        if to is not None:
            argv += ["--to", to]
        if reason is not None:
            argv += ["--reason", reason]
        argv += list(extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = handover_owner.main(argv)
        return code, out.getvalue(), err.getvalue()

    def owner(self):
        return self.ok("get_ticket", self.scope())["owner_session_id"]

    def notice_payload(self, attempt, session):
        return {**self.scope(), "attempt_id": attempt, "session_id": session,
                "origin_session_id": EXECUTOR, "status": "sent",
                "target_session_id": session, "wake_kind": "queued",
                "detail": "已把回执发回接替后的负责人"}

    # --- C3 的现场：接替之后 owner 是一条活会话 ---------------------------

    def test_handover_after_claim_puts_a_live_session_in_charge(self):
        attempt = self.dispatched()
        self.assertEqual(ARCHIVED, self.owner())
        code, out, err = self.run_tool()
        self.assertEqual(0, code, err)
        self.assertIn(handover_owner.HANDOVER_DONE, out)
        ticket = self.ok("get_ticket", self.scope())
        # owner 换成活会话，origin 一个字没改：谁派的仍是历史事实。
        self.assertEqual(RESIDENT, ticket["owner_session_id"])
        self.assertEqual(ARCHIVED, ticket["attempts"][-1]["coordinator_session_id"])
        # 同一现场用新 owner 的 session_id 记回执：过得去。
        notice = self.ok("record_notice", self.notice_payload(attempt, RESIDENT))
        self.assertEqual("sent", notice["status"])
        self.assertEqual(RESIDENT, self.ok("get_ticket", self.scope())["notices"][0]["target_session_id"])

    def test_handover_leaves_a_queryable_event(self):
        self.dispatched()
        self.run_tool()
        rows = [row for row in self.ok("events", self.scope(limit=100))["items"]
                if row["operation"] == "take_over_ticket"]
        self.assertEqual(1, len(rows))
        detail = rows[0]["detail"]["result"]
        self.assertEqual(ARCHIVED, detail["previous_owner_session_id"])
        self.assertEqual(RESIDENT, detail["owner_session_id"])
        self.assertIn("归档", detail["reason"])

    def test_dry_run_decides_without_writing(self):
        self.dispatched()
        code, out, err = self.run_tool("--dry-run")
        self.assertEqual(0, code, err)
        self.assertIn("没有写库", out)
        self.assertEqual(ARCHIVED, self.owner())

    # --- 不该接的一条也不许过，且一行不写 --------------------------------

    def test_refuses_while_the_owner_is_still_alive(self):
        """现场二：owner 还活着时被另一个会话接走，两个会话都以为自己该写合并记录。"""
        build_sessions(self.sessions_db, [
            (ARCHIVED, "还在跑的派单器", "{{WORKSPACE}}", "active", "schedule",
             STAMP_MS, STAMP_MS, STAMP_MS, None),
            (RESIDENT, "AI工作区值班", "{{WORKSPACE}}", "active", "user",
             STAMP_MS, STAMP_MS, STAMP_MS, None),
        ], reset=True)
        self.dispatched()
        code, out, _ = self.run_tool()
        self.assertEqual(1, code)
        self.assertIn("OWNER_STILL_ACTIVE", out)
        self.assertEqual(ARCHIVED, self.owner())

    def test_refuses_a_target_that_is_gone(self):
        self.dispatched()
        for dead in ("coordinator-3", "never-existed"):
            code, out, _ = self.run_tool(to=dead)
            self.assertEqual(1, code, out)
            self.assertIn("TARGET_NOT_ALIVE", out)
        self.assertEqual(ARCHIVED, self.owner())

    def test_refuses_when_only_the_merge_is_left(self):
        """收尾只剩合并时接替毫无意义：合并只看 done 与 notices 非空，不认会话。"""
        attempt = self.delivered()
        self.ok("record_notice", self.notice_payload(attempt, ARCHIVED))
        code, out, _ = self.run_tool()
        self.assertEqual(1, code)
        self.assertIn("NOTHING_TO_TAKE_OVER", out)
        self.assertIn("auto_merge.py", out)
        self.assertEqual(ARCHIVED, self.owner())

    def test_refuses_when_already_merged(self):
        attempt = self.delivered()
        self.ok("record_notice", self.notice_payload(attempt, ARCHIVED))
        self.ok("merge_ticket", {**self.scope(), "commit": "a1b2c3d", "target_branch": "master",
                                 "tests": "python tools/run_tests.py：全绿", "reviewed_sha": HEAD_SHA})
        code, out, _ = self.run_tool()
        self.assertEqual(1, code)
        self.assertIn("NOTHING_TO_TAKE_OVER", out)
        self.assertEqual(ARCHIVED, self.owner())

    def test_refuses_the_current_owner_and_the_executor(self):
        self.dispatched()
        code, out, _ = self.run_tool(to=ARCHIVED)
        self.assertEqual(1, code)
        self.assertIn("ALREADY_OWNER", out)
        code, out, _ = self.run_tool(to=EXECUTOR)
        self.assertEqual(1, code)
        self.assertIn("TAKEOVER_BY_EXECUTOR", out)
        self.assertEqual(ARCHIVED, self.owner())

    def test_refuses_a_cancelled_ticket_and_an_empty_reason(self):
        self.ticket()
        self.ok("claim_ticket", self.scope(expected_revision=1, coordinator_session_id=ARCHIVED))
        self.ok("cancel_ticket", self.scope(expected_revision=1, reason="不做了"))
        code, out, _ = self.run_tool()
        self.assertEqual(1, code)
        self.assertIn("CANCELLED", out)
        code, out, _ = self.run_tool(reason="   ")
        self.assertEqual(1, code)
        self.assertIn("REASON_REQUIRED", out)

    def test_unknown_is_not_a_yes(self):
        """宿主会话库读不出来时退出 2，绝不当成「可以接」。"""
        self.dispatched()
        argv = ["--ticket", self.ids["t1"], "--db", str(self.db),
                "--sessions-db", str(self.root / "absent.db"),
                "--to", RESIDENT, "--reason", "想接"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = handover_owner.main(argv)
        self.assertEqual(2, code)
        self.assertIn(handover_owner.UNKNOWN, err.getvalue())
        self.assertEqual(ARCHIVED, self.owner())


if __name__ == "__main__":
    unittest.main()
