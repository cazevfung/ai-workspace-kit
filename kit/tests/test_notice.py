"""schema 8：回信落库——协调者工作目录、回信结果与「发给谁、宿主怎么处理」。

这一批用例判的都是「库里说得清」，对应 AW-051 的 C2–C8 与 AW-060 的 C1–C5：

1. `claim_ticket` 接受可选的 `coordinator_workdir`，`get_ticket` 读得出来；不传就是空。
2. `notices` 字段够用：ticket_id、attempt_id、origin_session_id、status、detail、created_at，
   以及 8 版新增的 target_session_id 与 wake_kind。
3. `record_notice` 只收 `sent` / `failed`，回执带 `notice_id`。
4. 只由协调会话调：`session_id` 不是该尝试的 `coordinator_session_id` 就报 `NOT_COORDINATOR`，
   且一行都不写。
5. 一张单最多一条 `sent`（第二次报 `ALREADY_NOTIFIED` 且不写行）；`failed` 可以多条。
6. `failed` 不改工单状态，只留记录，回执带 `NOTIFY_FAILED` 等人来看。
7. 投递目标必须是协调会话：`target_session_id` 写成执行会话自己报 `NOT_TARGET`，不写行。
8. `wake_kind=created` 意味着宿主把它当成新建任务：一律落 `failed` 并报 `NOTIFY_CREATED_TASK`，
   命令非零退出，但那一行必须留在库里（事后要查得到）。
9. `wake_kind` 不是白名单：宿主回 `already-active` 这类枚举外的值时原样入库，
   由调用方挑一个近似值就是把事实换成推定；只校验形状（非空、无空白、≤ 40 字）。

这里不测「消息有没有真发出去」：本工具只做落库，投递不在范围内。
"""
import contextlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from task_center import Center

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import notice_payload  # noqa: E402  工具本体：C9 要在进程内注入「stat 与开库之间」的窗口


class NoticeTest(unittest.TestCase):
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
        self.ok("init")
        self.ok("register_project", {"project_id": "p1", "name": "项目一", "root": str(self.project)})

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

    def scope(self, **extra):
        return {"project_id": "p1", "ticket_id": "t1", **extra}

    def ticket(self, ticket_id="t1"):
        return self.ok("create_ticket", {
            "ticket_id": ticket_id, "project_id": "p1", "title": "整理检查清单",
            "deliverable": "一份逐项可勾选的检查清单",
            "prd_path": "PRD.md", "criteria": [{"id": "C1", "text": "包含明确的检查步骤"}],
        })

    def dispatched(self, ticket_id="t1", coordinator="coordinator-1", workdir=None, session="executor-1",
                   allow_parallel=False):
        """开单 → claim（可选带工作目录）→ bind，返回 attempt_id。"""
        payload = self.scope(ticket_id=ticket_id, expected_revision=1, coordinator_session_id=coordinator)
        if workdir is not None:
            payload["coordinator_workdir"] = workdir
        if allow_parallel:
            payload["allow_parallel"] = True
        claim = self.ok("claim_ticket", payload)
        self.ok("bind_session", self.scope(ticket_id=ticket_id, attempt_id=claim["attempt_id"],
                                           session_id=session, project_root=str(self.project)))
        return claim["attempt_id"], claim

    def notice_payload(self, attempt, status="sent", session="coordinator-1", ticket_id="t1", **extra):
        return {"project_id": "p1", "ticket_id": ticket_id, "attempt_id": attempt,
                "session_id": session, "origin_session_id": "origin-session-1",
                "status": status, "target_session_id": "coordinator-1", "wake_kind": "queued",
                "detail": "已把回执发回原会话" if status == "sent" else "目标会话已归档，发送失败",
                **extra}

    def notices(self, ticket_id="t1"):
        return self.ok("get_ticket", self.scope(ticket_id=ticket_id))["notices"]

    # --- C2：协调者工作目录 ------------------------------------------------

    def test_claim_records_optional_coordinator_workdir(self):
        self.ticket()
        _, with_workdir = self.dispatched(workdir="D:/别的项目")
        self.assertEqual("D:/别的项目", with_workdir["coordinator_workdir"])
        ticket = self.ok("get_ticket", self.scope())
        self.assertEqual("D:/别的项目", ticket["attempts"][0]["coordinator_workdir"])

        self.ticket("t2")
        # 本用例只验工作目录落不落库；两张单共存是前提，显式越过串行（AW-061）。
        _, without = self.dispatched("t2", session="executor-2", allow_parallel=True)
        self.assertIsNone(without["coordinator_workdir"])
        self.assertIsNone(self.ok("get_ticket", self.scope(ticket_id="t2"))["attempts"][0]["coordinator_workdir"])

    def test_claim_rejects_a_bad_workdir_without_claiming(self):
        self.ticket()
        for bad in ("", "   ", 7, "x" * 1001):
            self.error("INVALID_INPUT", "claim_ticket", self.scope(
                expected_revision=1, coordinator_session_id="coordinator-1", coordinator_workdir=bad))
        self.error("INVALID_INPUT", "claim_ticket", self.scope(
            expected_revision=1, coordinator_session_id="coordinator-1", coordinator_workdir=None))
        # 全部被拒之后工单还在 ready，一次都没被领走。
        self.assertEqual("ready", self.ok("get_ticket", self.scope())["state"])
        self.assertEqual([], self.ok("get_ticket", self.scope())["attempts"])

    # --- C3：表结构 --------------------------------------------------------

    def test_schema_eight_has_notices_with_target_and_wake_kind(self):
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(9, conn.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(
                ["id", "ticket_id", "attempt_id", "origin_session_id", "status", "detail",
                 "created_at", "target_session_id", "wake_kind"],
                [row[1] for row in conn.execute("PRAGMA table_info(notices)")])
            self.assertIn("coordinator_workdir",
                          [row[1] for row in conn.execute("PRAGMA table_info(attempts)")])
            index = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='one_sent_notice'").fetchone()[0]
            self.assertIn("status='sent'", index)
        finally:
            conn.close()

    # --- C4：record_notice 可用 -------------------------------------------

    def test_record_notice_writes_a_row_and_returns_its_id(self):
        self.ticket()
        attempt, _ = self.dispatched(workdir="D:/协调者")
        receipt = self.ok("record_notice", self.notice_payload(attempt))
        self.assertTrue(receipt["notice_id"])
        self.assertEqual("sent", receipt["status"])
        self.assertEqual("running", receipt["ticket_state"])
        self.assertIsNone(receipt["alert"])
        rows = self.notices()
        self.assertEqual(1, len(rows))
        self.assertEqual(receipt["notice_id"], rows[0]["id"])
        self.assertEqual(attempt, rows[0]["attempt_id"])
        self.assertEqual("origin-session-1", rows[0]["origin_session_id"])
        self.assertEqual("coordinator-1", rows[0]["target_session_id"])
        self.assertEqual("queued", rows[0]["wake_kind"])
        self.assertEqual("sent", rows[0]["status"])
        self.assertTrue(rows[0]["created_at"])

    def test_record_notice_accepts_only_sent_and_failed(self):
        self.ticket()
        attempt, _ = self.dispatched()
        for bad in ("ok", "SENT", "", 7):
            self.error("INVALID_INPUT", "record_notice", self.notice_payload(attempt, status=bad))
        self.error("INVALID_INPUT", "record_notice",
                   {k: v for k, v in self.notice_payload(attempt).items() if k != "detail"})
        self.error("INVALID_INPUT", "record_notice", self.notice_payload(attempt, note="多传字段"))
        self.error("INVALID_INPUT", "record_notice", self.notice_payload(attempt, origin_session_id=" "))
        self.error("INVALID_INPUT", "record_notice",
                   {k: v for k, v in self.notice_payload(attempt).items() if k != "target_session_id"})
        self.error("INVALID_INPUT", "record_notice",
                   {k: v for k, v in self.notice_payload(attempt).items() if k != "wake_kind"})
        for bad in ("", "  ", 7, None):
            self.error("INVALID_INPUT", "record_notice", self.notice_payload(attempt, target_session_id=bad))
        for bad in ("", "   ", 7, None, "x" * 41, "already active", "already\nactive"):
            self.error("INVALID_INPUT", "record_notice", self.notice_payload(attempt, wake_kind=bad))
        self.error("STALE_ATTEMPT", "record_notice", self.notice_payload("nope"))
        self.error("PROJECT_MISMATCH", "record_notice", {**self.notice_payload(attempt), "project_id": "p2"})
        self.assertEqual([], self.notices())
        # 写操作要 request_id，落到回执之前先记不下来。
        self.assertEqual("INVALID_INPUT",
                         self.center.execute("record_notice", self.notice_payload(attempt))["error"]["code"])

    # --- C5：只有协调会话能记 ---------------------------------------------
    def test_only_the_coordinator_may_record_a_notice(self):
        self.ticket()
        attempt, _ = self.dispatched()
        for session in ("executor-1", "coordinator-2"):
            self.error("NOT_COORDINATOR", "record_notice",
                       self.notice_payload(attempt, session=session))
        # 空字符串连「谁在调」都说不清，先报字段不合法，拿不到 NOT_COORDINATOR。
        self.error("INVALID_INPUT", "record_notice", self.notice_payload(attempt, session=" "))
        self.assertEqual([], self.notices())
        # 被拒之后协调会话照样能记：拒绝没有留下半行。
        self.ok("record_notice", self.notice_payload(attempt))
        self.assertEqual(1, len(self.notices()))

    # --- C6：一张单最多一条 sent，failed 可多条 ---------------------------

    def test_only_one_sent_notice_per_ticket_while_failures_repeat(self):
        self.ticket()
        attempt, _ = self.dispatched()
        first_failure = self.ok("record_notice", self.notice_payload(attempt, status="failed"))
        self.assertEqual("NOTIFY_FAILED", first_failure["alert"])
        second_failure = self.ok("record_notice",
                                 self.notice_payload(attempt, status="failed",
                                                     detail="重试仍然失败：目标会话不存在"))
        self.assertNotEqual(first_failure["notice_id"], second_failure["notice_id"])
        self.ok("record_notice", self.notice_payload(attempt))
        self.error("ALREADY_NOTIFIED", "record_notice", self.notice_payload(attempt))
        rows = self.notices()
        self.assertEqual(["failed", "failed", "sent"], [row["status"] for row in rows])
        self.assertEqual("重试仍然失败：目标会话不存在", rows[1]["detail"])

    # --- C7：失败不改工单状态 ---------------------------------------------

    def test_a_failed_notice_leaves_the_ticket_state_alone(self):
        self.ticket()
        attempt, _ = self.dispatched()
        before = self.ok("get_ticket", self.scope())
        receipt = self.ok("record_notice", self.notice_payload(attempt, status="failed"))
        self.assertEqual("NOTIFY_FAILED", receipt["alert"])
        self.assertEqual("running", receipt["ticket_state"])
        after = self.ok("get_ticket", self.scope())
        self.assertEqual(before["state"], after["state"])
        self.assertEqual(before["revision"], after["revision"])
        self.assertEqual("running", after["attempts"][0]["state"])
        self.assertEqual(1, len(after["notices"]))
        self.assertEqual("failed", after["notices"][0]["status"])

    # --- C8：AC06 的证据能跑通 -------------------------------------------

    def test_get_ticket_shows_notice_and_coordinator_workdir_together(self):
        self.ticket()
        attempt, _ = self.dispatched(workdir="D:/别的项目")
        self.ok("record_notice", self.notice_payload(
            attempt, status="failed", detail="目标会话已归档，发送失败"))
        ticket = self.ok("get_ticket", self.scope())
        self.assertEqual("D:/别的项目", ticket["attempts"][0]["coordinator_workdir"])
        self.assertEqual(1, len(ticket["notices"]))
        self.assertEqual("failed", ticket["notices"][0]["status"])
        self.assertEqual("目标会话已归档，发送失败", ticket["notices"][0]["detail"])
        self.assertEqual("running", ticket["state"])


    # --- AW-060 C2：投递目标与唤醒方式 ------------------------------------

    def test_target_must_be_the_coordinator_session(self):
        self.ticket()
        attempt, _ = self.dispatched()
        for target in ("executor-1", "coordinator-2", "origin-session-1"):
            self.error("NOT_TARGET", "record_notice", self.notice_payload(attempt, target_session_id=target))
        self.assertEqual([], self.notices())
        receipt = self.ok("record_notice", self.notice_payload(attempt))
        self.assertEqual("coordinator-1", receipt["target_session_id"])
        self.assertEqual("queued", receipt["wake_kind"])

    def test_resumed_is_accepted_as_a_wake_kind(self):
        self.ticket()
        attempt, _ = self.dispatched()
        receipt = self.ok("record_notice", self.notice_payload(attempt, wake_kind="resumed"))
        self.assertEqual("resumed", receipt["wake_kind"])
        self.assertEqual("resumed", self.notices()[0]["wake_kind"])

    def test_a_value_outside_the_known_set_is_recorded_verbatim(self):
        """宿主比白名单宽：`already-active` 这类取值原样入库，不映射成近似的三值之一。"""
        self.ticket()
        attempt, _ = self.dispatched()
        receipt = self.ok("record_notice", self.notice_payload(attempt, wake_kind="already-active"))
        self.assertEqual("already-active", receipt["wake_kind"])
        self.assertEqual("sent", receipt["status"])
        self.assertIsNone(receipt["alert"])
        rows = self.notices()
        self.assertEqual(1, len(rows))
        self.assertEqual("already-active", rows[0]["wake_kind"])
        # 原样入库，不做大小写或连字符改写：事后查「宿主到底回了什么」要能对得上。
        self.assertEqual("already-active", self.notices()[0]["wake_kind"])

    def test_wake_kind_boundaries_still_reject_without_writing_a_row(self):
        self.ticket()
        attempt, _ = self.dispatched()
        rejected = {"": "空串", "   ": "纯空白", "x" * 41: "41 字",
                    "already active": "内部空白", "already\nactive": "换行"}
        for bad in rejected:
            self.error("INVALID_INPUT", "record_notice", self.notice_payload(attempt, wake_kind=bad))
        # 40 字是边界内的，接受。
        self.ok("record_notice", self.notice_payload(attempt, wake_kind="x" * 40))
        self.assertEqual(["x" * 40], [row["wake_kind"] for row in self.notices()])

    def test_created_lands_a_failed_row_and_the_call_fails(self):
        self.ticket()
        attempt, _ = self.dispatched()
        # 调用方写的是 sent，但只要宿主回执说 created，这次回执就没到任何人手里。
        result = self.call("record_notice", self.notice_payload(attempt, status="sent", wake_kind="created"),
                           "created-once")
        self.assertFalse(result["ok"], result)
        self.assertEqual("NOTIFY_CREATED_TASK", result["error"]["code"])
        self.assertEqual("NOTIFY_CREATED_TASK", result["result"]["alert"])
        self.assertEqual("failed", result["result"]["status"])
        rows = self.notices()
        self.assertEqual(1, len(rows))
        self.assertEqual("failed", rows[0]["status"])
        self.assertEqual("created", rows[0]["wake_kind"])
        # 同一 request_id 重试拿到同一份失败回执，不产生第二行（回执已存库）。
        again = self.call("record_notice", self.notice_payload(attempt, status="sent", wake_kind="created"),
                          "created-once")
        self.assertEqual(result, again)
        self.assertEqual(1, len(self.notices()))
        # created 行不算 sent：「一张单最多一条 sent」不受影响，协调会话仍然能记一条 sent。
        self.ok("record_notice", self.notice_payload(attempt, wake_kind="queued"))
        self.assertEqual(["failed", "sent"], [row["status"] for row in self.notices()])

    def test_cli_exits_nonzero_on_a_created_wake_kind(self):
        self.ticket()
        attempt, _ = self.dispatched()
        payload = self.root / "created.json"
        payload.write_text(json.dumps(self.notice_payload(attempt, wake_kind="created")), encoding="utf-8")
        done = subprocess.run(
            [sys.executable, "-m", "task_center", "--db", str(self.db), "record_notice",
             "--request-id", "created-cli", "--input", str(payload)],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("NOTIFY_CREATED_TASK", done.stdout)
        # 非零退出但那一行在库里，而不是被回滚掉。
        self.assertEqual(["failed"], [row["status"] for row in self.notices()])


class NoticeToolsTest(unittest.TestCase):
    """两个只读工具：生成投递参数、认出没绑工单的会话（AW-060 C4 / C5）。"""

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
        self.ok("register_project", {"project_id": "p1", "name": "项目一", "root": str(self.project)})

    def call(self, op, payload=None, request_id=None):
        self.counter += 1
        return self.center.execute(op, payload or {}, request_id or f"req-{self.counter}")

    def ok(self, op, payload=None, request_id=None):
        result = self.call(op, payload, request_id)
        self.assertTrue(result["ok"], result)
        return result["result"]

    def ticket(self, ticket_id="t1"):
        return self.ok("create_ticket", {
            "ticket_id": ticket_id, "project_id": "p1", "title": "整理检查清单",
            "deliverable": "一份逐项可勾选的检查清单", "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "包含明确的检查步骤"}],
        })

    def run_tool(self, name, *args):
        return subprocess.run([sys.executable, str(ROOT / "tools" / name), *args],
                              cwd=ROOT, capture_output=True, text=True, encoding="utf-8")

    def test_notice_payload_prints_send_and_record_blocks(self):
        self.ticket()
        claim = self.ok("claim_ticket", {"project_id": "p1", "ticket_id": "t1", "expected_revision": 1,
                                          "coordinator_session_id": "coordinator-1"})
        self.ok("bind_session", {"project_id": "p1", "ticket_id": "t1", "attempt_id": claim["attempt_id"],
                                 "session_id": "executor-1", "project_root": str(self.project)})
        done = self.run_tool("notice_payload.py", "--ticket", "t1", "--db", str(self.db))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        for needle in ('"target_session_id": "coordinator-1"', '"wake_kind": "queued"',
                       '"message"', claim["attempt_id"], "record_notice", "send_to_session"):
            self.assertIn(needle, done.stdout)

    def test_notice_payload_refuses_a_ticket_with_no_coordinator(self):
        self.ticket()
        done = self.run_tool("notice_payload.py", "--ticket", "t1", "--db", str(self.db))
        self.assertEqual(2, done.returncode, done.stdout)
        self.assertIn("claim_ticket", done.stderr)
        missing = self.run_tool("notice_payload.py", "--ticket", "absent", "--db", str(self.db))
        self.assertEqual(2, missing.returncode)
        self.assertIn("工单不存在", missing.stderr)

    def bind(self):
        self.ticket()
        claim = self.ok("claim_ticket", {"project_id": "p1", "ticket_id": "t1", "expected_revision": 1,
                                          "coordinator_session_id": "coordinator-1"})
        self.ok("bind_session", {"project_id": "p1", "ticket_id": "t1", "attempt_id": claim["attempt_id"],
                                 "session_id": "executor-1", "project_root": str(self.project)})
        return claim

    def side_files(self):
        return sorted(p.name for p in self.db.parent.iterdir()
                      if p.name.startswith(self.db.name + "-") and p.name.endswith(("-wal", "-shm")))

    def test_notice_payload_leaves_no_side_files_when_no_wal(self):
        """库旁没有非空 `-wal` 时走 `immutable=1`：跑完一个旁文件都不多（AW-064 C4）。

        这个断言最容易被打破的方式是「为了方便统一退回 mode=ro」——那条路每跑一次
        都会在真库旁边留下 `-shm`，而这是每次派发都要跑一遍的工具。
        """
        self.bind()
        self.assertEqual([], self.side_files(), "夹具本身不该带 -wal/-shm，否则测的是另一条路")
        done = self.run_tool("notice_payload.py", "--ticket", "t1", "--db", str(self.db))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("immutable=1", done.stdout, "输出要写明这次走的是不留旁文件那条路")
        self.assertEqual([], self.side_files(), "没有 -wal 时不许在真库旁边留旁文件")

    def test_notice_payload_reads_new_value_from_uncheckpointed_wal(self):
        """库旁有非空 `-wal` 时必须读到新值，宁可留旁文件（AW-064 C1–C2）。

        AW-062 验收实测过的错法：无条件 `immutable=1`，4152 字节未 checkpoint 时
        读到旧 attempt 与旧 state。这里先把差写进 WAL，再用 `immutable=1` 自己读一次
        （断言它确实读到旧值），然后跑工具断言读到新值——两条路都在同一个库上跑，
        证明差别来自读法，不是夹具偶然。
        """
        self.bind()
        writer = sqlite3.connect(self.db, timeout=10, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("BEGIN")
        for _ in range(300):  # 把 WAL 撑到几十页，保证不是「0 字节 -wal」那条路
            writer.execute(
                "INSERT INTO events(ticket_id,operation,detail,created_at)"
                " VALUES ('t1','pad',?, '2026-11-18T00:00:00+00:00')", ("x" * 500,))
        writer.execute("COMMIT")
        writer.execute("BEGIN")
        writer.execute("UPDATE tickets SET state='awaiting_review' WHERE id='t1'")
        writer.execute("COMMIT")
        wal = self.db.with_name(self.db.name + "-wal")
        self.assertGreater(wal.stat().st_size, 4096, "-wal 必须非空，否则这条用例没测到重点")

        main_only = sqlite3.connect(self.db.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            old = main_only.execute("SELECT state FROM tickets WHERE id='t1'").fetchone()[0]
        finally:
            main_only.close()
        self.assertNotEqual("awaiting_review", old, "immutable=1 应该读到旧值，这是这条用例的前提")

        done = self.run_tool("notice_payload.py", "--ticket", "t1", "--db", str(self.db))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("mode=ro", done.stdout, "有非空 -wal 时必须退回 mode=ro 并说明理由")
        self.assertIn("状态 awaiting_review", done.stdout, f"读到旧值就不算修好：{done.stdout}")

    def test_notice_payload_recovers_when_wal_lands_between_stat_and_open(self):
        """C9：stat 与开库之间 `-wal` 才落地，也要读到新值而不是静默读旧值。

        验收实测过这个窗口：stat 时没有 `-wal`，开库前 156592 字节落地，工具 exit=0
        却输出旧 state 与旧 attempt，读取方式那行还写着 `immutable=1`。
        这里用 `_wal_bytes` 的两次取值把那个窗口确定地复现出来（第一次说 0、第二次说
        真实大小），断言工具自己发现并重开——而不是靠下一次跑对。
        """
        self.bind()
        writer = sqlite3.connect(self.db, timeout=10, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("BEGIN")
        for _ in range(300):
            writer.execute(
                "INSERT INTO events(ticket_id,operation,detail,created_at)"
                " VALUES ('t1','pad',?, '2026-11-18T00:00:00+00:00')", ("x" * 500,))
        writer.execute("COMMIT")
        writer.execute("BEGIN")
        writer.execute("UPDATE tickets SET state='awaiting_review' WHERE id='t1'")
        writer.execute("COMMIT")
        landed = notice_payload._wal_bytes(self.db)
        self.assertGreater(landed, 4096, "-wal 必须非空，否则这条用例没测到重点")

        out = io.StringIO()
        with mock.patch.object(notice_payload, "_wal_bytes", side_effect=[0, landed]):
            with contextlib.redirect_stdout(out):
                code = notice_payload.main(["--ticket", "t1", "--db", str(self.db)])

        text = out.getvalue()
        self.assertEqual(0, code, text)
        self.assertIn("mode=ro", text, f"发现 -wal 落地后必须改走 mode=ro：{text}")
        self.assertIn("窗口", text, "要说清这是第二次核对才发现的变化")
        self.assertNotIn("读取方式：immutable=1", text, "不许一边读到旧值一边宣称 immutable")
        self.assertIn("状态 awaiting_review", text, f"读到旧值就不算收掉窗口：{text}")

    def test_session_roster_reports_a_bound_executor(self):
        self.ticket()
        claim = self.ok("claim_ticket", {"project_id": "p1", "ticket_id": "t1", "expected_revision": 1,
                                          "coordinator_session_id": "coordinator-1"})
        self.ok("bind_session", {"project_id": "p1", "ticket_id": "t1", "attempt_id": claim["attempt_id"],
                                 "session_id": "executor-1", "project_root": str(self.project)})
        done = self.run_tool("session_roster.py", "--session", "executor-1", "--db", str(self.db))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("t1", done.stdout)
        self.assertIn("running", done.stdout)
        self.assertNotIn("NOT_BOUND", done.stdout)

    def test_session_roster_reports_not_bound_for_a_session_with_no_ticket(self):
        ghost = "<session-id>"
        done = self.run_tool("session_roster.py", "--session", ghost, "--db", str(self.db))
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("NOT_BOUND", done.stdout)
        self.assertIn("你不是任何工单的执行者，不要推进流程", done.stdout)

    def test_session_roster_reports_not_bound_after_the_attempt_is_terminal(self):
        self.ticket()
        claim = self.ok("claim_ticket", {"project_id": "p1", "ticket_id": "t1", "expected_revision": 1,
                                          "coordinator_session_id": "coordinator-1"})
        self.ok("bind_session", {"project_id": "p1", "ticket_id": "t1", "attempt_id": claim["attempt_id"],
                                 "session_id": "executor-1", "project_root": str(self.project)})
        (self.project / "t1-result.md").write_text("1. 检查步骤", encoding="utf-8")
        submission = self.ok("submit_result", {
            "project_id": "p1", "ticket_id": "t1", "attempt_id": claim["attempt_id"],
            "session_id": "executor-1", "artifacts": ["t1-result.md"], "summary": "清单已完成",
            "evidence": ["人工检查条目"], "risks": [],
            "head_sha": "0123456789abcdef0123456789abcdef01234567",
        })
        self.ok("review_result", {
            "project_id": "p1", "ticket_id": "t1", "attempt_id": claim["attempt_id"],
            "submission_id": submission["submission_id"], "reviewer_session_id": "reviewer-1",
            "verdict": "accept", "checks": [{"id": "C1", "passed": True, "evidence": "已核对"}],
            "reason": "逐项检查后作出结论",
        })
        done = self.run_tool("session_roster.py", "--session", "executor-1", "--db", str(self.db))
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("NOT_BOUND", done.stdout)
        self.assertIn("已终结", done.stdout)


if __name__ == "__main__":
    unittest.main()
