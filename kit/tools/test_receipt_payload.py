#!/usr/bin/env python
"""`tools/receipt_payload.py` 的用例：一份能直接复制的 args，加几条必须拒绝的路。

判据只看三样东西：stdout 里的那两段（`send_to_session` 与 `说明`）、退出码、
以及 stderr 里那句原因。**不看实现细节**（查了哪张表、拆成几个函数），因为那些
换一次重构就全变，而「收件人取对了」「缺 id 时拒绝」是这个工具存在的全部理由。

两个反例各造一次，各自对应一条验收条件：

- `test_no_coordinator_refuses_instead_of_sending_nowhere`：尝试上没有
  `coordinator_session_id` 时必须非零退出、且**一个 args 都不打印**——打印出来的
  东西一定会被复制走，没有 target 的那份会被当成「省略 target」发出去，宿主就
  新建一条任务（C3 举的就是这个反例）。
- `test_db_is_not_written`：只读工具不许在库上留下字节变化（这是它敢在真库旁边跑
  的前提）。

跑法：

    python tools/test_receipt_payload.py     # 单独跑，用例名逐条打印
    python tools/run_tests.py                # 随主线套件跑
"""
import contextlib
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
if str(TOOLS.parent) not in sys.path:
    sys.path.insert(0, str(TOOLS.parent))

import notice_payload
import receipt_payload

from task_center.db import SCHEMA_VERSION

PROJECT = "ai-workspace"
COORDINATOR = "74fb606a-24bd-42d9-b464-2092f364ca7c"
COORDINATOR_OLD = "0ldc0ord-0000-0000-0000-000000000000"
EXECUTOR = "570c9a6e-4b62-4bb2-a1a1-9f11d4130cdc"
OWNER_NEW = "9a1f00wner-1111-2222-3333-444455556666"
ATTEMPT = "b98bb61c-f569-4d45-9d3c-5a90164375f7"
ATTEMPT_OLD = "11111111-2222-3333-4444-555555555555"
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"


def create_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE tickets (
            id TEXT PRIMARY KEY, project_id TEXT, title TEXT, deliverable TEXT,
            prd_path TEXT, prd_sha256 TEXT, criteria TEXT, revision INTEGER,
            criteria_version INTEGER, state TEXT, current_attempt TEXT,
            created_at TEXT, batch_id TEXT, owner_session_id TEXT
        );
        CREATE TABLE attempts (
            id TEXT PRIMARY KEY, ticket_id TEXT, revision INTEGER, prd_path TEXT,
            prd_sha256 TEXT, state TEXT, session_id TEXT, created_at TEXT,
            coordinator_session_id TEXT, coordinator_workdir TEXT,
            criteria_version INTEGER, batch_key TEXT
        );
        CREATE TABLE submissions (
            id TEXT PRIMARY KEY, attempt_id TEXT, summary TEXT, artifacts TEXT,
            evidence TEXT, risks TEXT, created_at TEXT, head_sha TEXT,
            base_sha TEXT, conflict_preview TEXT
        );
        -- 这个脚本不读合并记录，但共用的那条只读读法（notice_payload.read_ticket）读，
        -- 少了这张表就整条路都报 no such table。
        CREATE TABLE merges (
            id TEXT PRIMARY KEY, ticket_id TEXT, commit_sha TEXT, target_branch TEXT,
            tests TEXT, created_at TEXT, reviewed_sha TEXT
        );
        """
    )
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return conn


class Fixture(unittest.TestCase):
    """每个用例一条临时库：库里只有这个脚本真正读的那几列。"""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.db_path = self.root / "task-center.sqlite3"
        self.conn = create_db(self.db_path)
        self.addCleanup(self._temp.cleanup)

    def tearDown(self):
        self.conn.close()

    # ---- 造数据

    def add_ticket(self, ticket_id="AW-111", state="awaiting_review",
                   current_attempt=ATTEMPT, project_id=PROJECT, owner=None):
        self.conn.execute(
            "INSERT INTO tickets (id, project_id, title, deliverable, prd_path,"
            " prd_sha256, criteria, revision, criteria_version, state,"
            " current_attempt, created_at, batch_id, owner_session_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticket_id, project_id, f"{ticket_id} 的标题", "交付物",
             "PRD/PRD-001-Cindy会话工作体系.md", "a" * 64, "[]", 1, 1, state,
             current_attempt, "2026-11-22T00:00:00+00:00", None, owner),
        )

    def add_attempt(self, attempt_id=ATTEMPT, ticket_id="AW-111",
                    coordinator=COORDINATOR, state="running"):
        self.conn.execute(
            "INSERT INTO attempts (id, ticket_id, revision, prd_path, prd_sha256,"
            " state, session_id, created_at, coordinator_session_id,"
            " coordinator_workdir, criteria_version, batch_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (attempt_id, ticket_id, 1, "PRD/PRD-001-Cindy会话工作体系.md", "a" * 64,
             state, EXECUTOR, "2026-11-22T00:00:00+00:00", coordinator,
             "{{WORKSPACE}}", 1, None),
        )

    def add_submission(self, submission_id="sub-1", attempt_id=ATTEMPT,
                       head_sha=HEAD_SHA):
        self.conn.execute(
            "INSERT INTO submissions (id, attempt_id, summary, artifacts, evidence,"
            " risks, created_at, head_sha, base_sha, conflict_preview)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (submission_id, attempt_id, "改了什么", "[]", "自检", "[]",
             "2026-11-22T00:00:00+00:00", head_sha, "b" * 40, None),
        )

    # ---- 跑工具

    def run_tool(self, *extra):
        argv = ["--ticket", "AW-111", "--db", str(self.db_path), *extra]
        out, err = io.StringIO(), io.StringIO()
        self.conn.commit()  # 让同进程里的只读连接看见刚造的行
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = receipt_payload.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    # ---- 读输出

    @staticmethod
    def block(stdout, name):
        """取出 `--- name ---` 与下一段 `--- … ---` 之间的行。没有这一段返回 None。"""
        lines = stdout.splitlines()
        marker = f"--- {name} ---"
        if marker not in lines:
            return None
        start = lines.index(marker) + 1
        body = []
        for line in lines[start:]:
            if line.startswith("--- ") and line.endswith(" ---"):
                break
            body.append(line)
        return "\n".join(body).strip()

    def send_args(self, stdout):
        body = self.block(stdout, "send_to_session")
        self.assertIsNotNone(body, "输出里没有 send_to_session 那一段")
        return json.loads(body)


class PrintPayload(Fixture):
    def test_prints_two_blocks_with_coordinator_target(self):
        """C1：两段都在，且 target_session_id 就是该尝试的 coordinator_session_id。"""
        self.add_ticket()
        self.add_attempt()
        self.add_submission()
        code, out, err = self.run_tool()
        self.assertEqual(0, code, err)
        args = self.send_args(out)
        self.assertEqual(COORDINATOR, args["target_session_id"])
        self.assertEqual({"message", "target_session_id"}, set(args))
        self.assertIsNotNone(self.block(out, "说明"), "输出里没有说明那一段")

    def test_the_target_is_the_current_owner_not_the_origin(self):
        """C1：owner 与 origin 不相等时，收件人是 owner——回执该去的地方是「现在能收尾的那条」。"""
        self.add_ticket(owner=OWNER_NEW)
        self.add_attempt(coordinator=COORDINATOR)
        self.add_submission()
        code, out, err = self.run_tool()
        self.assertEqual(0, code, err)
        self.assertEqual(OWNER_NEW, self.send_args(out)["target_session_id"])
        note = self.block(out, "说明")
        self.assertIn(OWNER_NEW, note)
        self.assertIn(COORDINATOR, note, "说明里要写清 origin 是谁，它是历史，不再参与投递")

    def test_without_an_owner_the_target_falls_back_to_the_origin(self):
        """老库（schema 15 以前）没有 owner 这一列：行为与以前逐字一致。"""
        self.add_ticket(owner=None)
        self.add_attempt(coordinator=COORDINATOR)
        self.add_submission()
        code, out, err = self.run_tool()
        self.assertEqual(0, code, err)
        self.assertEqual(COORDINATOR, self.send_args(out)["target_session_id"])

    def test_message_carries_only_ticket_state_and_head_sha(self):
        """正文只写三个事实：工单号、状态、提交 sha。"""
        self.add_ticket(state="awaiting_review")
        self.add_attempt()
        self.add_submission()
        code, out, _ = self.run_tool()
        self.assertEqual(0, code)
        message = self.send_args(out)["message"]
        self.assertIn("AW-111", message)
        self.assertIn("awaiting_review", message)
        self.assertIn(HEAD_SHA, message)
        # attempt id 与 revision 是「几小时内就失效」的东西，不进正文。
        self.assertNotIn(ATTEMPT, message)
        self.assertNotIn("revision", message)

    def test_default_attempt_is_the_current_one(self):
        """没给 --attempt 时取 current_attempt，不是「最后插进去的那条」。"""
        self.add_ticket(current_attempt=ATTEMPT)
        self.add_attempt()
        self.add_attempt(attempt_id=ATTEMPT_OLD, coordinator=COORDINATOR_OLD)
        code, out, _ = self.run_tool()
        self.assertEqual(0, code)
        self.assertEqual(COORDINATOR, self.send_args(out)["target_session_id"])

    def test_explicit_attempt_wins(self):
        self.add_ticket(current_attempt=ATTEMPT)
        self.add_attempt()
        self.add_attempt(attempt_id=ATTEMPT_OLD, coordinator=COORDINATOR_OLD)
        code, out, _ = self.run_tool("--attempt", ATTEMPT_OLD)
        self.assertEqual(0, code)
        self.assertEqual(COORDINATOR_OLD, self.send_args(out)["target_session_id"])

    def test_attempt_of_another_ticket_is_refused(self):
        self.add_ticket()
        self.add_attempt()
        self.add_attempt(attempt_id="other-attempt", ticket_id="AW-999")
        code, out, err = self.run_tool("--attempt", "other-attempt")
        self.assertEqual(2, code)
        self.assertEqual("", out)
        self.assertIn("other-attempt", err)

    def test_message_says_so_when_nothing_submitted_yet(self):
        """还没提交也能取参数（先看一遍流程），但正文不能凭空写一个 sha。"""
        self.add_ticket(state="running")
        self.add_attempt()
        code, out, _ = self.run_tool()
        self.assertEqual(0, code)
        message = self.send_args(out)["message"]
        self.assertIn("AW-111", message)
        self.assertNotIn(HEAD_SHA, message)
        self.assertIn("没有提交", message)

    def test_说明_says_how_to_call_and_why_target_is_mandatory(self):
        """C1 的那一段说明要写清：怎么调（call_tool）、为什么不能省 target。"""
        self.add_ticket()
        self.add_attempt()
        self.add_submission()
        _, out, _ = self.run_tool()
        note = self.block(out, "说明")
        self.assertIn("call_tool", note)
        self.assertIn("工具列表里可能搜不到", note)
        self.assertIn("target_session_id", note)
        self.assertIn("新建任务", note)


class Refusals(Fixture):
    def test_no_coordinator_refuses_instead_of_sending_nowhere(self):
        """C3：没有协调会话标识时非零退出、说清原因，且一个 args 都不打印。"""
        self.add_ticket()
        self.add_attempt(coordinator=None)
        self.add_submission()
        code, out, err = self.run_tool()
        self.assertEqual(2, code)
        self.assertEqual("", out, "拒绝时不许打印任何可被复制的 args")
        self.assertIn("coordinator_session_id", err)
        self.assertIn("AW-111", err)

    def test_ticket_without_attempts_is_refused(self):
        self.add_ticket()
        code, out, err = self.run_tool()
        self.assertEqual(2, code)
        self.assertEqual("", out)
        self.assertIn("claim_ticket", err)

    def test_unknown_ticket_is_refused(self):
        code, out, err = self.run_tool()
        self.assertEqual(2, code)
        self.assertEqual("", out)
        self.assertIn("AW-111", err)

    def test_project_mismatch_is_refused(self):
        self.add_ticket(project_id=PROJECT)
        self.add_attempt()
        code, out, err = self.run_tool("--project", "cindy-harmony")
        self.assertEqual(2, code)
        self.assertEqual("", out)
        self.assertIn(PROJECT, err)


class ReadOnly(Fixture):
    def test_db_is_not_written(self):
        """只读工具：跑完库文件逐字节不变。"""
        self.add_ticket()
        self.add_attempt()
        self.add_submission()
        self.conn.commit()
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        code, _, _ = self.run_tool()
        self.assertEqual(0, code)
        after = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)


class DefaultDb(unittest.TestCase):
    """默认库只在主检出里有一份：在 `.worktrees/<工单号>/` 里跑也必须找到它。

    找不到时报「中心库不存在」是一句不成立的话——那不是数据问题，是路径找错了，
    而 C1 给的那条命令是不带 `--db` 的。
    """

    def test_local_data_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".data").mkdir()
            db = root / ".data" / "task-center.sqlite3"
            db.write_bytes(b"")
            old = notice_payload.ROOT
            notice_payload.ROOT = root
            try:
                self.assertEqual(db.resolve(), notice_payload.default_ticket_db().resolve())
            finally:
                notice_payload.ROOT = old

    def test_worktree_falls_back_to_main_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "main"
            main.mkdir()
            subprocess.run(["git", "-C", str(main), "init"], check=True,
                           capture_output=True)
            (main / ".data").mkdir()
            db = main / ".data" / "task-center.sqlite3"
            db.write_bytes(b"")
            work = main / ".worktrees" / "AW-111"
            work.mkdir(parents=True)
            old = notice_payload.ROOT
            notice_payload.ROOT = work
            try:
                self.assertEqual(db.resolve(), notice_payload.default_ticket_db().resolve())
            finally:
                notice_payload.ROOT = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
