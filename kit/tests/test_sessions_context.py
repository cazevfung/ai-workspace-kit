"""PRD-015 R5：一张单牵扯到哪些会话、哪条消息。

这批用例判的是「一个会话拿到一张单时，上下文里就能看到谁派、谁做、谁验、回执发给了谁」，
不是「再存一份会话标题」。四条硬口径：

1. `get_ticket` 的 `sessions` 覆盖四层角色（requester / coordinator / executor / reviewer），
   外加回执收件人 `receipt_target`；空会话标识不产出条目，也不产出 `cindy://session/` 空链接。
2. `session_url` 的形状是 `cindy://session/<id>?device=<deviceId>`；`device` 取不到时写 `null`，
   链接退回不带参数的形态——不拼一个假 device（本机 `agent_device_id` 全是 NULL）。
3. 会话被归档/删除时 `session_state` 与 `stale` 说得清；宿主库读不到时是 `unknown` 而不是
   「已失效」或「还活着」。
4. 消息级只存 id：`notices.message_id` 是唯一载体，读法由 PRD 写明（`get_chat_history`），
   库里不存正文、也不生成 message deeplink。

迁移用例单独一组：从一个真正的 v13 库迁到当前版本（现在这条链是 13 → 14 加 `requester_session_id` 与
`message_id`、14 → 15 加 `owner_session_id` 并回填），只加列加回填，不猜值、不少行。
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from task_center import Center, hostsessions
from task_center import db as tdb

CINDY_SCHEMA = """
CREATE TABLE sessions(
 id TEXT PRIMARY KEY NOT NULL,
 title TEXT DEFAULT '' NOT NULL,
 status TEXT DEFAULT 'active' NOT NULL,
 agent_device_id TEXT);
"""


def build_cindy_db(path, rows):
    """造一个最小的宿主库：只有本模块读的那三列。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(CINDY_SCHEMA)
        conn.executemany("INSERT INTO sessions(id,title,status,agent_device_id) VALUES (?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()
    return path


class CenterCase(unittest.TestCase):
    """一套夹具：项目、PRD、开单 → 领 → 绑 → 交 → 验 → 回执。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "中文项目"
        self.project.mkdir()
        (self.project / "PRD.md").write_text("需求第一版", encoding="utf-8")
        (self.project / "成果.txt").write_text("做完了", encoding="utf-8")
        self.db = self.root / "center.sqlite3"
        self.center = Center(self.db)
        self.counter = 0
        self.ids = {}
        self.ok("init")
        self.ok("register_project", {"project_id": "p1", "name": "项目一", "root": str(self.project)})
        # 宿主库默认指到一个不存在的路径：本机真库不该成为测试的输入。
        self.host_db = self.root / "cindy.db"
        self.host_env = mock.patch.dict(os.environ, {"TASK_CENTER_CINDY_DB": str(self.host_db)})
        self.host_env.start()
        self.addCleanup(self.host_env.stop)

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
        return result["error"]

    def scope(self, **extra):
        return {"project_id": "p1", "ticket_id": "t1", **extra}

    def ticket(self, alias="t1", **extra):
        created = self.ok("create_ticket", {
            "project_id": "p1", "title": "整理检查清单",
            "deliverable": "一份逐项可勾选的检查清单",
            "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
            **extra,
        })
        self.ids[alias] = created["id"]
        return created

    def dispatched(self, ticket_id="t1", coordinator="coordinator-1", session="executor-1"):
        claim = self.ok("claim_ticket", self.scope(ticket_id=ticket_id, expected_revision=1,
                                                   coordinator_session_id=coordinator))
        self.ok("bind_session", self.scope(ticket_id=ticket_id, attempt_id=claim["attempt_id"],
                                           session_id=session, project_root=str(self.project)))
        return claim["attempt_id"]

    def submitted(self, attempt, session="executor-1"):
        self.ok("submit_result", self.scope(ticket_id="t1", attempt_id=attempt, session_id=session,
                                            summary="做完了",
                                            artifacts=["成果.txt"],
                                            evidence=["python -m unittest tests.test_sessions_context -v → OK"],
                                            risks=["无"], head_sha="a" * 40, base_sha="b" * 40))

    def get(self, alias="t1"):
        return self.ok("get_ticket", {"project_id": "p1", "ticket_id": alias})

    def sessions(self, alias="t1"):
        return self.get(alias)["sessions"]

    @staticmethod
    def by_role(rows, role):
        return [row for row in rows if row["role"] == role]


class MigrationTest(unittest.TestCase):
    """v13 库迁到当前版本：只加列加回填，行数不变。14 加的 `requester_session_id` / `notices.message_id`
    对存量行留 NULL；15 加的 `owner_session_id` 按当前尝试的 `coordinator_session_id` 回填，没有尝试的单留 NULL。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "center.sqlite3"

    def v13_database(self):
        """用当前代码建库，再把 14 与 15 才有的列都去掉（`requester_session_id` / `message_id` 是 14 加的，
        `owner_session_id` 是 15 加的），然后 `PRAGMA user_version=13` 造出一个真正的 v13 形状。"""
        Center(self.db).execute("init", {}, "init-1")
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute("INSERT INTO projects(id,name,root,next_number) VALUES ('p1','项目一','/tmp/p1',1)")
            conn.execute(
                "INSERT INTO tickets(id,project_id,title,deliverable,prd_path,prd_sha256,criteria,revision,"
                "criteria_version,state,current_attempt,created_at,requester_session_id)"
                " VALUES ('AW-001','p1','旧单','旧成果','PRD.md',NULL,'[]',1,1,'ready',NULL,'2026-11-01',NULL)")
            conn.execute("INSERT INTO attempts(id,ticket_id,revision,criteria_version,prd_path,prd_sha256,state,"
                         "session_id,coordinator_session_id,created_at) VALUES ('a1','AW-001',1,1,'PRD.md','x',"
                         "'submitted','exec-1','coord-1','2026-11-01')")
            conn.execute("INSERT INTO notices(id,ticket_id,attempt_id,origin_session_id,status,detail,created_at,"
                         "target_session_id,wake_kind,message_id) VALUES ('n1','AW-001','a1','coord-1','sent',"
                         "'回执','2026-11-01','coord-1','queued',NULL)")
            conn.execute("ALTER TABLE tickets DROP COLUMN requester_session_id")
            conn.execute("ALTER TABLE tickets DROP COLUMN owner_session_id")
            conn.execute("ALTER TABLE notices DROP COLUMN message_id")
            # 16 才加的 merges 四列也要去掉，否则这个夹具不是真正的 v13 形状。
            for column in ("source", "verified_by", "verified_at", "note"):
                conn.execute(f"ALTER TABLE merges DROP COLUMN {column}")
            conn.execute("PRAGMA user_version=13")
            conn.commit()
        finally:
            conn.close()

    def test_upgrade_from_v13_keeps_rows_and_leaves_the_new_columns_null(self):
        self.v13_database()
        result = Center(self.db).execute("migrate", {}, "mig-1")
        self.assertTrue(result["ok"], result)
        self.assertEqual(13, result["result"]["from_version"])
        self.assertEqual(tdb.SCHEMA_VERSION, result["result"]["schema_version"])
        conn = sqlite3.connect(str(self.db))
        try:
            self.assertEqual(tdb.SCHEMA_VERSION, conn.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(1, conn.execute("SELECT count(*) FROM tickets").fetchone()[0])
            self.assertEqual(1, conn.execute("SELECT count(*) FROM notices").fetchone()[0])
            # 存量行留 NULL：那时没有这一项，补一个猜测值就是推定事实。
            self.assertIsNone(conn.execute("SELECT requester_session_id FROM tickets").fetchone()[0])
            self.assertIsNone(conn.execute("SELECT message_id FROM notices").fetchone()[0])
        finally:
            conn.close()

    def test_13_claiming_the_new_columns_is_refused(self):
        """旧版本号配新形状：拒绝，且一个字节都不改。"""
        self.v13_database()
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute("ALTER TABLE tickets ADD COLUMN requester_session_id TEXT")
            conn.commit()
        finally:
            conn.close()
        result = Center(self.db).execute("migrate", {}, "mig-2")
        self.assertFalse(result["ok"], result)
        self.assertEqual("SCHEMA_MISMATCH", result["error"]["code"])


class HostSessionsTest(unittest.TestCase):
    """宿主库这一层只读、只在能读的时候回答，读不到一律空表。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def read(self, path):
        with mock.patch.dict(os.environ, {"TASK_CENTER_CINDY_DB": str(path)}):
            return hostsessions.host_sessions()

    def test_missing_database_is_an_empty_map(self):
        self.assertEqual({}, self.read(self.root / "没有这个库.db"))

    def test_unreadable_file_is_an_empty_map(self):
        junk = self.root / "不是库.db"
        junk.write_text("这不是 sqlite", encoding="utf-8")
        self.assertEqual({}, self.read(junk))

    def test_database_without_sessions_table_is_an_empty_map(self):
        empty = self.root / "空库.db"
        sqlite3.connect(str(empty)).close()
        self.assertEqual({}, self.read(empty))

    def test_reads_device_and_state(self):
        path = build_cindy_db(self.root / "cindy.db", [
            ("s-archived", "旧会话", "archived", "DEV-1"),
            ("s-active", "在跑的", "active", None),
        ])
        got = self.read(path)
        self.assertEqual({"device": "DEV-1", "state": "archived"}, got["s-archived"])
        self.assertEqual({"device": None, "state": "active"}, got["s-active"])

    def test_returns_nothing_without_the_env_override(self):
        """没有覆盖时去宿主目录找；找不到就是空表，不抛异常。"""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TASK_CENTER_CINDY_DB", None)
            with mock.patch.object(hostsessions, "cindy_dir", return_value=self.root / "没有目录"):
                self.assertEqual({}, hostsessions.host_sessions())


class SessionsBlockTest(CenterCase):
    """`get_ticket` 的 sessions 块：四层角色、链接形状、失效口径。"""

    def full_ticket(self):
        self.ticket(requester_session_id="requester-1")
        attempt = self.dispatched()
        self.submitted(attempt)
        self.ok("review_result", self.scope(ticket_id="t1", attempt_id=attempt,
                                            submission_id=self.get()["submissions"][-1]["id"],
                                            reviewer_session_id="reviewer-1", verdict="accept",
                                            checks=[{"id": "C1", "passed": True, "evidence": "跑过了"}],
                                            reason="逐项核过"))

    def notice(self, attempt, **extra):
        return self.ok("record_notice", self.scope(ticket_id="t1", attempt_id=attempt,
                                                   session_id="coordinator-1",
                                                   origin_session_id="coordinator-1",
                                                   status="sent", detail="回执已发",
                                                   target_session_id="coordinator-1", **extra))

    def test_five_roles_answer_who_is_who(self):
        self.full_ticket()
        attempt = self.get()["attempts"][-1]["id"]
        self.notice(attempt, message_id="msg-77", wake_kind="queued")
        rows = self.sessions()
        self.assertEqual(["requester", "coordinator", "executor", "reviewer", "receipt_target"],
                         [row["role"] for row in rows])

    def test_every_item_carries_the_same_eight_keys(self):
        self.full_ticket()
        for row in self.sessions():
            self.assertEqual({"role", "session_id", "session_url", "device", "session_state",
                              "stale", "at", "source"} | ({"message_id"} if row["role"] == "receipt_target" else set()),
                             set(row))

    def test_session_ids_match_the_columns_they_came_from(self):
        self.full_ticket()
        attempt = self.get()["attempts"][-1]
        review = self.get()["reviews"][-1]
        rows = {row["role"]: row for row in self.sessions()}
        self.assertEqual(attempt["coordinator_session_id"], rows["coordinator"]["session_id"])
        self.assertEqual(attempt["session_id"], rows["executor"]["session_id"])
        self.assertEqual(review["reviewer_session_id"], rows["reviewer"]["session_id"])
        # 请求方不是协调会话的别名（C4 的反例正是把两者当同一个）。
        self.assertEqual("requester-1", rows["requester"]["session_id"])
        self.assertNotEqual(rows["coordinator"]["session_id"], rows["requester"]["session_id"])

    def test_url_carries_the_host_device_when_known(self):
        build_cindy_db(self.host_db, [("coordinator-1", "调度", "active", "DEV-9")])
        self.full_ticket()
        row = self.by_role(self.sessions(), "coordinator")[0]
        self.assertEqual("DEV-9", row["device"])
        self.assertEqual("cindy://session/coordinator-1?device=DEV-9", row["session_url"])

    def test_url_has_no_invented_device_when_unknown(self):
        self.full_ticket()
        for row in self.sessions():
            self.assertIsNone(row["device"])
            self.assertEqual(f"cindy://session/{row['session_id']}", row["session_url"])
            self.assertNotIn("device=", row["session_url"])

    def test_archived_session_is_marked_stale(self):
        build_cindy_db(self.host_db, [("executor-1", "做完了", "archived", "DEV-9")])
        self.full_ticket()
        row = self.by_role(self.sessions(), "executor")[0]
        self.assertEqual("archived", row["session_state"])
        self.assertTrue(row["stale"])

    def test_unreadable_host_database_is_unknown_not_stale(self):
        self.full_ticket()
        row = self.by_role(self.sessions(), "reviewer")[0]
        self.assertEqual("unknown", row["session_state"])
        self.assertFalse(row["stale"])

    def test_empty_session_ids_produce_no_dead_links(self):
        self.ticket()
        rows = self.sessions()
        self.assertEqual([], rows)
        self.assertNotIn("cindy://session/", json.dumps(rows, ensure_ascii=False))

    def test_message_id_is_stored_as_an_id_and_read_back(self):
        self.ticket()
        attempt = self.dispatched()
        self.notice(attempt, message_id="msg-77", wake_kind="queued")
        row = self.by_role(self.sessions(), "receipt_target")[0]
        self.assertEqual("msg-77", row["message_id"])
        self.assertEqual("msg-77", self.get()["notices"][-1]["message_id"])

    def test_message_id_is_optional(self):
        self.ticket()
        attempt = self.dispatched()
        self.notice(attempt, wake_kind="queued")
        self.assertIsNone(self.get()["notices"][-1]["message_id"])
        self.assertIsNone(self.by_role(self.sessions(), "receipt_target")[0]["message_id"])

    def test_requester_is_optional_and_never_faked(self):
        self.ticket()
        attempt = self.dispatched()
        self.assertEqual([], self.by_role(self.sessions(), "requester"))

    def test_blank_requester_is_refused(self):
        self.error("INVALID_INPUT", "create_ticket", {
            "project_id": "p1", "title": "整理检查清单", "deliverable": "一份清单",
            "prd_path": "PRD.md", "requester_session_id": "   ",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        })

    def test_message_id_must_be_one_token(self):
        self.ticket()
        attempt = self.dispatched()
        self.error("INVALID_INPUT", "record_notice", self.scope(
            ticket_id="t1", attempt_id=attempt, session_id="coordinator-1",
            origin_session_id="coordinator-1", status="sent", detail="回执已发",
            target_session_id="coordinator-1", wake_kind="queued", message_id="msg 77"))


if __name__ == "__main__":
    unittest.main()
