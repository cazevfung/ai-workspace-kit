"""schema 5：验收通过的工单需要一条可查询的「已合并」记录。

`done` 只说「验收通过」，不说「结果已经进了目标分支」。没有合并记录时，这两件事
只能靠人记得：`tasks/plan-005-merge-after-accept.md` 的起因就是功能验收了、代码还在
分支上。这里给 `merges` 表加两道口子——写入用 `merge_ticket`（只接受 done、同一工单
只能一条），读取用 `list_unmerged`（done 但没有合并行的工单）。

历史工单 AW-004~AW-016 是直接在 master 上做的，没有 `merges` 行，`list_unmerged`
会照样列出它们。这是事实，不是缺陷：清单的作用是让人看见并判断，而不是替人补记录。
"""
import hashlib
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from task_center import Center

# 冻结的 schema 4：与 v5 的差别只有 merges 表。既然是历史形状，就不要跟着
# task_center.db.SCHEMA 一起改。
V4_SCHEMA = """
CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, root TEXT NOT NULL UNIQUE);
CREATE TABLE tickets(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), title TEXT NOT NULL,
 deliverable TEXT NOT NULL,
 prd_path TEXT NOT NULL, prd_sha256 TEXT NOT NULL, criteria TEXT NOT NULL,
 revision INTEGER NOT NULL, state TEXT NOT NULL, current_attempt TEXT, created_at TEXT NOT NULL);
CREATE TABLE attempts(
 id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id), revision INTEGER NOT NULL,
 prd_path TEXT NOT NULL, prd_sha256 TEXT NOT NULL,
 state TEXT NOT NULL, session_id TEXT, coordinator_session_id TEXT, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX active_session ON attempts(session_id)
 WHERE session_id IS NOT NULL AND state IN ('running','submitted');
CREATE TABLE submissions(
 id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id),
 summary TEXT NOT NULL, artifacts TEXT NOT NULL, evidence TEXT NOT NULL, risks TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE reviews(
 id TEXT PRIMARY KEY, submission_id TEXT NOT NULL UNIQUE REFERENCES submissions(id),
 reviewer_session_id TEXT NOT NULL, verdict TEXT NOT NULL, checks TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT REFERENCES tickets(id),
 operation TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX events_ticket ON events(ticket_id, id);
CREATE INDEX tickets_project ON tickets(project_id, created_at);
CREATE TABLE requests(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL);
"""

V4_TABLES = ("projects", "tickets", "attempts", "submissions", "reviews", "events", "requests")
STAMP = "2026-11-17T00:00:00+00:00"
# 本文件里「被验收的那棵树」的 sha。真值由 `git rev-parse HEAD` 给，这里只要合法：
# 用例验的是「提交记下来了、合并时比对得上/对不上」，不是 sha 本身对不对。
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"


def build_v4(path, project_root):
    """写一个内容完整的 v4 库：一张 running 工单、一张已验收的工单、事件与回执都有。"""
    prd_sha = hashlib.sha256((Path(project_root) / "PRD.md").read_bytes()).hexdigest()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(V4_SCHEMA)
        conn.execute("INSERT INTO projects VALUES ('p1','项目一',?)", (str(project_root),))
        conn.execute(
            "INSERT INTO tickets VALUES ('t1','p1','已经验收的活','一份清单','PRD.md',?,'[]',1,'done','a1',?)",
            (prd_sha, STAMP))
        conn.execute(
            "INSERT INTO tickets VALUES ('t2','p1','还在做','另一份清单','PRD.md',?,'[]',1,'running','a2',?)",
            (prd_sha, STAMP))
        conn.execute("INSERT INTO attempts VALUES ('a1','t1',1,'PRD.md',?,'accepted','executor-1','coordinator-1',?)",
                     (prd_sha, STAMP))
        conn.execute("INSERT INTO attempts VALUES ('a2','t2',1,'PRD.md',?,'running','executor-2','coordinator-1',?)",
                     (prd_sha, STAMP))
        conn.execute("INSERT INTO submissions VALUES ('s1','a1','已交付','[]','[\"自检\"]','[]',?)", (STAMP,))
        conn.execute("INSERT INTO reviews VALUES ('r1','s1','reviewer-1','accept','[]','逐项核对',?)", (STAMP,))
        conn.execute("INSERT INTO events(ticket_id,operation,detail,created_at) VALUES ('t1','review_result','{}',?)",
                     (STAMP,))
        conn.execute("INSERT INTO requests VALUES ('req-1','fp','{}')")
        conn.execute("PRAGMA user_version=4")
        conn.commit()
    finally:
        conn.close()
    return path


def counts(path, tables=V4_TABLES):
    conn = sqlite3.connect(path)
    try:
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
    finally:
        conn.close()


def user_version(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def table_names(path):
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "项目一"
        self.project.mkdir()
        (self.project / "PRD.md").write_text("需求第一版", encoding="utf-8")
        self.other = self.root / "项目二"
        self.other.mkdir()
        (self.other / "PRD.md").write_text("另一份需求", encoding="utf-8")
        self.db = self.root / "center.sqlite3"
        self.center = Center(self.db)
        self.counter = 0
        # 库发号之后，测试里仍用 t1 / t2 指代工单；resolve() 发请求前换成库发的号。
        self.ids = {}
        self.ok("init")
        self.ok("register_project", {"project_id": "p1", "name": "项目一", "root": str(self.project)})
        self.ok("register_project", {"project_id": "p2", "name": "项目二", "root": str(self.other)})

    def call(self, op, payload=None, request_id=None):
        self.counter += 1
        return self.center.execute(op, self.resolve(payload or {}), request_id or f"req-{self.counter}")

    def resolve(self, payload):
        """把 payload 里的短名换成库发的工单号（create_ticket 不再收自报号）。"""
        alias = payload.get("ticket_id")
        real = self.ids.get(alias) if isinstance(alias, str) else None
        return {**payload, "ticket_id": real} if real else payload

    def tid(self, alias="t1"):
        return self.ids[alias]

    def ok(self, op, payload=None, request_id=None):
        result = self.call(op, payload, request_id)
        self.assertTrue(result["ok"], result)
        return result["result"]

    def error(self, code, op, payload=None, request_id=None):
        result = self.call(op, payload, request_id)
        self.assertFalse(result["ok"], result)
        self.assertEqual(code, result["error"]["code"], result)

    def scope(self, **extra):
        return {"project_id": "p1", "ticket_id": "t1", **extra}

    def ticket(self, ticket_id="t1", project_id="p1"):
        created = self.ok("create_ticket", {
            "project_id": project_id, "title": "整理检查清单",
            "deliverable": "一份逐项可勾选的检查清单",
            "prd_path": "PRD.md", "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        })
        self.ids[ticket_id] = created["id"]
        return created

    def done(self, ticket_id="t1", project_id="p1", executor="executor-1", reviewer="reviewer-1"):
        """走完整流程把一张工单做到 done：领取、绑定、提交、独立验收。"""
        self.ticket(ticket_id, project_id)
        scope = {"project_id": project_id, "ticket_id": ticket_id}
        claim = self.ok("claim_ticket", {**scope, "expected_revision": 1,
                                        "coordinator_session_id": "coordinator-1"})
        self.ok("bind_session", {**scope, "attempt_id": claim["attempt_id"],
                                 "session_id": executor, "project_root": str(self.project)})
        (self.project / f"{ticket_id}-result.md").write_text("1. 检查步骤", encoding="utf-8")
        submission = self.ok("submit_result", {
            **scope, "attempt_id": claim["attempt_id"], "session_id": executor,
            "artifacts": [f"{ticket_id}-result.md"], "summary": "清单已完成",
            "evidence": ["人工检查条目"], "risks": [], "head_sha": HEAD_SHA,
            "base_sha": "89abcdef0123456789abcdef0123456789abcdef",
        })
        return self.ok("review_result", {
            **scope, "attempt_id": claim["attempt_id"], "submission_id": submission["submission_id"],
            "reviewer_session_id": reviewer, "verdict": "accept",
            "checks": [{"id": "C1", "passed": True, "evidence": "已阅读清单并核对步骤"}],
            "reason": "逐项检查后作出结论",
        })

    def merge_payload(self, **extra):
        return {"project_id": "p1", "ticket_id": "t1", "commit": "a1b2c3d",
                "target_branch": "master", "tests": "python tools/run_tests.py：全绿",
                "reviewed_sha": HEAD_SHA, **extra}

    def notice(self, ticket_id="t1", project_id="p1", status="sent", wake="queued"):
        """给已交付的工单记一条回执：schema 8 起没过回执的单写不进合并记录。"""
        ticket = self.ok("get_ticket", {"project_id": project_id, "ticket_id": ticket_id})
        return self.ok("record_notice", {
            "project_id": project_id, "ticket_id": ticket_id,
            "attempt_id": ticket["attempts"][-1]["id"], "session_id": "coordinator-1",
            "origin_session_id": "coordinator-1", "status": status,
            "detail": "已把回执发回原会话" if status == "sent" else "目标会话已归档，发送失败",
            "target_session_id": "coordinator-1", "wake_kind": wake,
        })

    # --- merge_ticket 写入 -------------------------------------------------

    def test_merge_only_accepts_a_done_ticket(self):
        self.ticket()
        self.error("INVALID_STATE", "merge_ticket", self.merge_payload())
        claim = self.ok("claim_ticket", self.scope(expected_revision=1, coordinator_session_id="coordinator-1"))
        self.error("INVALID_STATE", "merge_ticket", self.merge_payload())
        self.ok("bind_session", self.scope(attempt_id=claim["attempt_id"], session_id="executor-1",
                                          project_root=str(self.project)))
        self.error("INVALID_STATE", "merge_ticket", self.merge_payload())
        self.assertEqual([], self.ok("get_ticket", self.scope())["merges"])

    def test_merge_records_commit_branch_and_tests(self):
        self.done()
        self.notice()
        receipt = self.ok("merge_ticket", self.merge_payload(), "merge-once")
        self.assertEqual(self.tid("t1"), receipt["ticket_id"])
        self.assertEqual("a1b2c3d", receipt["commit"])
        self.assertEqual("master", receipt["target_branch"])
        self.assertEqual("python tools/run_tests.py：全绿", receipt["tests"])
        # 完全相同的重试返回当时的回执，不产生第二条记录。
        self.assertEqual(receipt, self.ok("merge_ticket", self.merge_payload(), "merge-once"))
        ticket = self.ok("get_ticket", self.scope())
        self.assertEqual("done", ticket["state"])
        self.assertEqual(1, len(ticket["merges"]))
        self.assertEqual("a1b2c3d", ticket["merges"][0]["commit_sha"])
        self.assertEqual(receipt["merge_id"], ticket["merges"][0]["id"])

    def test_second_merge_of_the_same_ticket_is_rejected(self):
        self.done()
        self.notice()
        self.ok("merge_ticket", self.merge_payload())
        self.error("ALREADY_MERGED", "merge_ticket", self.merge_payload())
        self.error("ALREADY_MERGED", "merge_ticket", self.merge_payload(commit="ffffff", target_branch="release"))
        self.assertEqual(1, len(self.ok("get_ticket", self.scope())["merges"]))

    def test_merge_requires_every_field_and_rejects_unknown_ones(self):
        self.done()
        for key in ("project_id", "ticket_id", "commit", "target_branch", "tests"):
            payload = self.merge_payload()
            payload.pop(key)
            self.error("INVALID_INPUT", "merge_ticket", payload)
        for key, value in (("ticket_id", "absent"), ("project_id", "p2")):
            self.error("NOT_FOUND" if key == "ticket_id" else "PROJECT_MISMATCH", "merge_ticket",
                       self.merge_payload(**{key: value}))
        for key in ("commit", "target_branch", "tests"):
            for bad in (None, "", "   ", 7):
                self.error("INVALID_INPUT", "merge_ticket", self.merge_payload(**{key: bad}))
        self.error("INVALID_INPUT", "merge_ticket", {**self.merge_payload(), "notes": "多余字段"})
        # 缺 request_id 的写入被拒，不落记录。
        result = self.center.execute("merge_ticket", self.merge_payload())
        self.assertEqual("INVALID_INPUT", result["error"]["code"])
        self.assertEqual([], self.ok("get_ticket", self.scope())["merges"])

    # --- AW-060 C3：没过回执的单合不进去 -----------------------------------

    def test_merge_without_a_notice_reports_no_notice(self):
        self.done()
        self.error("NO_NOTICE", "merge_ticket", self.merge_payload())
        self.assertEqual([], self.ok("get_ticket", self.scope())["merges"])
        self.assertEqual([self.tid("t1")], [item["id"] for item in self.ok("list_unmerged")["items"]])  # 还没合，仍列出
        # failed 也算走过回执：回执没送达但原因记下来了，这是事实，不是可越过的门。
        self.notice(status="failed")
        receipt = self.ok("merge_ticket", self.merge_payload())
        self.assertEqual(self.tid("t1"), receipt["ticket_id"])
        self.assertEqual(1, len(self.ok("get_ticket", self.scope())["merges"]))

    def test_already_merged_still_wins_over_the_notice_gate(self):
        self.done()
        self.notice()
        self.ok("merge_ticket", self.merge_payload())
        # 已有合并记录的单行为不变：先报 ALREADY_MERGED，不是 NO_NOTICE。
        self.error("ALREADY_MERGED", "merge_ticket", self.merge_payload())

    # --- AW-078：被验收的树与要合的树对齐 -------------------------------

    def test_merge_records_the_reviewed_sha(self):
        """C4 第一种：相等就放行，并把被验收的 sha 写进 merges。"""
        self.done()
        self.notice()
        self.ok("merge_ticket", self.merge_payload(), "merge-with-sha")
        merges = self.ok("get_ticket", self.scope())["merges"]
        self.assertEqual(1, len(merges))
        self.assertEqual(HEAD_SHA, merges[0]["reviewed_sha"])
        # 提交上写下的 sha 也读得回：这就是验收会话拿去 `git rev-parse HEAD` 核的那个值。
        self.assertEqual(HEAD_SHA, self.ok("get_ticket", self.scope())["submissions"][0]["head_sha"])

    def test_merge_refuses_a_sha_that_is_not_the_submitted_one(self):
        """C4 第二种：不等报 SHA_MISMATCH，且一行不写。

        这张单存在的理由就是这个场景：验收之后又提交了一次，合并照样通过，而库里查不出来。
        """
        self.done()
        self.notice()
        self.error("SHA_MISMATCH", "merge_ticket", self.merge_payload(reviewed_sha=OTHER_SHA))
        self.assertEqual([], self.ok("get_ticket", self.scope())["merges"])
        self.assertEqual([self.tid("t1")], [item["id"] for item in self.ok("list_unmerged")["items"]])
        # 校验顺序：大小写不影响比对（git 给的是小写，人手里可能大写）。
        self.ok("merge_ticket", self.merge_payload(reviewed_sha=HEAD_SHA.upper()), "merge-upper")

    def test_merge_legacy_submission_requires_the_unverified_literal(self):
        """C4 第三种：老提交的 head_sha 是 NULL。

        库里有 140 多条历史提交、49 张老单，它们的 head_sha 只能是 NULL。静默放行等于
        把「没核对过」写成「核对过」，全堵死又让老单永远合不了。所以要求调用方显式传字面量
        `unverified`，并把它原样入库。
        """
        self.done()
        self.notice()
        # 把这一行改成「历史提交」的样子：那时还没有 head_sha 这一列。
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE submissions SET head_sha=NULL")
        conn.commit()
        conn.close()
        self.assertIsNone(self.ok("get_ticket", self.scope())["submissions"][0]["head_sha"])
        # 不传字面量就合不进去，无论是另一个 sha 还是「什么都不说」。
        # 这里报 INVALID_INPUT 而不是 SHA_MISMATCH：没有可比的 sha 就不是「两边不等」，
        # 而是调用方给了一个这里不接受的値。
        self.error("INVALID_INPUT", "merge_ticket", self.merge_payload(reviewed_sha=HEAD_SHA))
        self.error("INVALID_INPUT", "merge_ticket", {k: v for k, v in self.merge_payload().items()
                                                    if k != "reviewed_sha"})
        self.assertEqual([], self.ok("get_ticket", self.scope())["merges"])
        self.ok("merge_ticket", self.merge_payload(reviewed_sha="unverified"), "merge-legacy")
        self.assertEqual("unverified", self.ok("get_ticket", self.scope())["merges"][0]["reviewed_sha"])

    def test_merge_rejects_unverified_when_a_head_sha_is_recorded(self):
        """有 sha 的单不许自称 unverified：那等于把一个可核对的事写成没核对。"""
        self.done()
        self.notice()
        self.error("INVALID_INPUT", "merge_ticket", self.merge_payload(reviewed_sha="unverified"))
        # 既不是 40 位十六进制也不是 unverified，同样拒绝。
        for bad in (None, "", HEAD_SHA[:39], HEAD_SHA + "0", "Z" * 40, "unverifie"):
            self.error("INVALID_INPUT", "merge_ticket", self.merge_payload(reviewed_sha=bad))
        self.assertEqual([], self.ok("get_ticket", self.scope())["merges"])

    # --- list_unmerged 读取 -------------------------------------------------

    def test_list_unmerged_lists_done_tickets_without_a_merge_record(self):
        self.done()
        self.ticket("t2")  # 还没开始做
        listed = self.ok("list_unmerged")
        self.assertEqual([self.tid("t1")], [item["id"] for item in listed["items"]])
        item = listed["items"][0]
        self.assertEqual("整理检查清单", item["title"])
        self.assertEqual("done", item["state"])
        self.assertEqual("p1", item["project_id"])
        self.assertFalse(item["has_merge"])
        self.notice()
        self.ok("merge_ticket", self.merge_payload())
        self.assertEqual([], self.ok("list_unmerged")["items"])

    def test_list_unmerged_keeps_old_tickets_that_were_never_merged(self):
        # 历史工单直接在 master 上做，没有 merges 行；清单要如实列出，不能替人隐去。
        self.done("t1")
        self.done("t2", executor="executor-2", reviewer="reviewer-2")
        self.notice()
        self.ok("merge_ticket", self.merge_payload())
        listed = self.ok("list_unmerged")
        self.assertEqual([self.tid("t2")], [item["id"] for item in listed["items"]])
        self.assertEqual(1, listed["count"])
        self.assertFalse(listed["items"][0]["has_merge"])

    def test_list_unmerged_filters_by_project(self):
        self.done("t1", project_id="p1")
        self.ticket("t2", project_id="p2")
        # p2 的工单还没做，不该出现在任何清单里。
        self.assertEqual([self.tid("t1")], [item["id"] for item in self.ok("list_unmerged", {"project_id": "p1"})["items"]])
        self.assertEqual([], self.ok("list_unmerged", {"project_id": "p2"})["items"])
        self.assertEqual([], self.ok("list_unmerged", {"project_id": "unknown"})["items"])
        for bad in (None, "", 7):
            self.error("INVALID_INPUT", "list_unmerged", {"project_id": bad})
        self.error("INVALID_INPUT", "list_unmerged", {"project_id": "p1", "limit": 1})

    # --- AW-087 / PRD-013 R4：只想看最近这几天的单 --------------------------

    def backdate(self, ticket_id, days):
        """把工单的 created_at 往前挪几天：只改副本库里的这一行。"""
        stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        # 连接必须关掉：Windows 上开着句柄的临时目录删不掉，用例会以清理错误收场。
        conn = sqlite3.connect(str(self.db))
        try:
            with conn:
                conn.execute("UPDATE tickets SET created_at=? WHERE id=?",
                             (stamp, self.ids.get(ticket_id, ticket_id)))
        finally:
            conn.close()

    def test_list_unmerged_since_days_keeps_only_recent_tickets(self):
        self.done("t1")
        self.done("t2", executor="executor-2", reviewer="reviewer-2")
        self.backdate("t2", 10)
        # 不传 since_days 时行为与现在完全一致：两张都在。
        self.assertEqual(sorted([self.tid("t1"), self.tid("t2")]),
                         sorted(item["id"] for item in self.ok("list_unmerged")["items"]))
        recent = self.ok("list_unmerged", {"since_days": 1})
        self.assertEqual([self.tid("t1")], [item["id"] for item in recent["items"]])
        self.assertEqual(1, recent["count"])
        # 10 天前的单放回够宽的口子照样看得到：不是被永久隐去。
        self.assertIn(self.tid("t2"), [item["id"] for item in self.ok("list_unmerged", {"since_days": 30})["items"]])

    def test_list_unmerged_rejects_a_since_days_out_of_range(self):
        self.done("t1")
        self.assertEqual([self.tid("t1")], [item["id"] for item in self.ok("list_unmerged", {"since_days": 365})["items"]])
        # 布尔是 int 的子类（True == 1），不能因为它看着像 1 就收。
        for bad in (0, 366, -1, True, "1", 1.5, None):
            self.error("INVALID_INPUT", "list_unmerged", {"since_days": bad})

    # --- migrate 4 -> 5 -> 6 -----------------------------------------------

    def test_migrate_from_v4_adds_merges_and_keeps_every_row(self):
        legacy = build_v4(self.root / "center-v4.sqlite3", self.project)
        before = counts(legacy)
        result = Center(legacy).execute("migrate", {})
        self.assertTrue(result["ok"], result)
        receipt = result["result"]
        self.assertEqual(4, receipt["from_version"])
        self.assertEqual(13, receipt["schema_version"])
        self.assertTrue(receipt["migrated"])
        self.assertEqual(13, user_version(legacy))
        self.assertIn("merges", table_names(legacy))
        self.assertEqual(before, counts(legacy))
        center = Center(legacy)
        ticket = center.execute("get_ticket", {"project_id": "p1", "ticket_id": "t1"})
        self.assertTrue(ticket["ok"], ticket)
        self.assertEqual("done", ticket["result"]["state"])
        self.assertEqual(["accept"], [item["verdict"] for item in ticket["result"]["reviews"]])
        self.assertEqual([], ticket["result"]["merges"])
        # 升级后的库能直接用新表：回执先落库，已验收的工单才能写合并记录，未完成的被拒。
        noticed = center.execute("record_notice", {
            "project_id": "p1", "ticket_id": "t1", "attempt_id": "a1",
            "session_id": "coordinator-1", "origin_session_id": "coordinator-1",
            "status": "sent", "detail": "已把回执发回原会话",
            "target_session_id": "coordinator-1", "wake_kind": "queued",
        }, "notice-after-migrate")
        self.assertTrue(noticed["ok"], noticed)
        # v4 库里没有 head_sha 这一列，那条提交是历史提交：按 legacy 路径显式传 unverified。
        merged = center.execute("merge_ticket", self.merge_payload(reviewed_sha="unverified"),
                                "merge-after-migrate")
        self.assertTrue(merged["ok"], merged)
        self.assertFalse(center.execute("merge_ticket", {**self.merge_payload(reviewed_sha="unverified"),
                                                        "ticket_id": "t2"},
                                        "merge-after-migrate-2")["ok"])
        unmerged = center.execute("list_unmerged", {})
        self.assertTrue(unmerged["ok"], unmerged)
        self.assertEqual([], unmerged["result"]["items"])
        # 再跑一次仍是「已是最新」，不改任何行。
        settled = counts(legacy)
        again = center.execute("migrate", {})
        self.assertTrue(again["ok"], again)
        self.assertFalse(again["result"]["migrated"])
        self.assertEqual(settled, counts(legacy))

    def test_migrate_rejects_a_v4_database_that_already_has_merges(self):
        legacy = build_v4(self.root / "center-v4-newer.sqlite3", self.project)
        conn = sqlite3.connect(legacy)
        conn.execute("CREATE TABLE merges(id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
        before = digest(legacy)
        result = Center(legacy).execute("migrate", {})
        self.assertFalse(result["ok"], result)
        self.assertEqual("SCHEMA_MISMATCH", result["error"]["code"])
        self.assertEqual(4, user_version(legacy))
        self.assertEqual(before, digest(legacy))


if __name__ == "__main__":
    unittest.main()
