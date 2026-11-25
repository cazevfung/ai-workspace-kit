#!/usr/bin/env python
"""`tools/dispatch_payload.py` 的用例：骨架段由库算、现场段由人写、缺了就一条都不打印。

判据只看三样东西：stdout（骨架五行 + 分隔线 + 现场段原文）、退出码、stderr 里那句
原因。**不看实现细节**（查了哪张表、拆成几个函数）——「骨架取对了」「现场段被原样
接上」「缺一节时 stdout 为空」才是这个工具存在的全部理由。

覆盖的几类：

- `Prints`：正常单输出骨架五块（协调会话 id 等于该单 `owner_session_id`）、回执那行
  给出 `receipt_payload.py --ticket <单号>`、现场段原文一字不改地跟在分隔线后面。
- `ContextChecks`：`--context` 缺失 / 文件不存在 / 空文件 / 缺一圈 / 某圈正文为空 /
  Why 只写「见 PRD」，每种都非零退出、stdout 为空、stderr 点名缺哪一节。
- `Refusals`：缺 owner、没有尝试、状态不对、项目没登记，同样非零且 stdout 为空。
- `NonGit`：项目根不是 git 仓库时出现 `repo_kind=non_git`，且不出现 `git worktree add`。

跑法：

    python tools/test_dispatch_payload.py     # 单独跑，用例名逐条打印
    python tools/run_tests.py                 # 随主线套件跑
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

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
if str(TOOLS.parent) not in sys.path:
    sys.path.insert(0, str(TOOLS.parent))

import dispatch_payload

from task_center.db import SCHEMA_VERSION

PROJECT = "ai-workspace"
TICKET = "AW-111"
OWNER = "7dd5ec21-cd63-417f-90f5-74a41f1dc895"
ATTEMPT = "bb3d335c-03c5-4000-8f50-c78f91eeba51"

SKELETON_LABELS = ("工单：", "协调会话 id：", "开工第一步：", "开工方式：", "回执怎么发：")
SEPARATOR = "--- 现场段（派单人写的，不是库里的字段） ---"

GOOD_CONTEXT = """# AW-111 派单现场

Why：上一轮把 icon 的位置改了又改，这一单要在动代码之前先把落点定死，否则返工三次仍会撞同一个位置。

How：只改 `tools/dispatch_payload.py` 与三处文档；`receipt_payload` 与 `record_notice` 的参数与语义一个字不动。

What：骨架段五行 + 现场段文件校验，以及三处文档指向同一条命令。

最容易做歪的地方：
1. 又抄一遍散文——三处各写一份新的模板。
2. 打印到一半再报错——半条消息被贴出去。
"""


def create_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, name TEXT, root TEXT, next_number INTEGER
        );
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
        -- 这个工具不读提交与合并记录，但共用的那条只读读法
        -- （notice_payload.read_ticket）读，少了这两张表整条路都报 no such table。
        CREATE TABLE submissions (
            id TEXT PRIMARY KEY, attempt_id TEXT, summary TEXT, artifacts TEXT,
            evidence TEXT, risks TEXT, created_at TEXT, head_sha TEXT,
            base_sha TEXT, conflict_preview TEXT
        );
        CREATE TABLE merges (
            id TEXT PRIMARY KEY, ticket_id TEXT, commit_sha TEXT, target_branch TEXT,
            tests TEXT, created_at TEXT, reviewed_sha TEXT
        );
        """
    )
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return conn


class Fixture(unittest.TestCase):
    """每个用例一条临时库、一个临时「项目根」、一份现场段。"""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.db_path = self.root / "task-center.sqlite3"
        self.project_root = self.root / "project"
        self.project_root.mkdir()
        self.context_path = self.root / "context.md"
        self.write_context(GOOD_CONTEXT)
        self.conn = create_db(self.db_path)
        self.addCleanup(self._temp.cleanup)

    def tearDown(self):
        self.conn.close()

    # ---- 造数据

    def write_context(self, text):
        self.context_path.write_text(text, encoding="utf-8")

    def add_project(self, root=None):
        self.conn.execute(
            "INSERT INTO projects (id, name, root, next_number) VALUES (?,?,?,?)",
            (PROJECT, "AI 工作区", str(root or self.project_root), 169),
        )

    def add_ticket(self, state="running", current_attempt=ATTEMPT, owner=OWNER,
                   criteria_version=2):
        self.conn.execute(
            "INSERT INTO tickets (id, project_id, title, deliverable, prd_path,"
            " prd_sha256, criteria, revision, criteria_version, state,"
            " current_attempt, created_at, batch_id, owner_session_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (TICKET, PROJECT, "派单消息由工具生成", "交付物",
             "PRD/PRD-001-Cindy会话工作体系.md", "a" * 64, "[]", 1,
             criteria_version, state, current_attempt,
             "2026-11-25T00:00:00+00:00", None, owner),
        )

    def add_attempt(self, attempt_id=ATTEMPT, state="running"):
        self.conn.execute(
            "INSERT INTO attempts (id, ticket_id, revision, prd_path, prd_sha256,"
            " state, session_id, created_at, coordinator_session_id,"
            " coordinator_workdir, criteria_version, batch_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (attempt_id, TICKET, 1, "PRD/PRD-001-Cindy会话工作体系.md", "a" * 64,
             state, None, "2026-11-25T00:00:00+00:00", OWNER, "{{WORKSPACE}}", 1, None),
        )

    def make_git_root(self):
        subprocess.run(["git", "init", "-q", str(self.project_root)], check=True,
                       capture_output=True)

    # ---- 跑工具

    def run_tool(self, *extra, context=True):
        argv = ["--ticket", TICKET, "--db", str(self.db_path)]
        if context:
            argv += ["--context", str(self.context_path)]
        argv += list(extra)
        out, err = io.StringIO(), io.StringIO()
        self.conn.commit()  # 让同进程里的只读连接看见刚造的行
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dispatch_payload.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def shown_root(self):
        """消息里的项目根统一写成正斜杠。"""
        return str(self.project_root).replace("\\", "/")

    def lines(self, stdout):
        return [line for line in stdout.splitlines() if line.strip()]


class Prints(Fixture):
    def test_skeleton_five_lines_then_context_verbatim(self):
        """C1/C5：骨架五行、协调会话 id 就是 owner、现场段原文跟在分隔线后面。"""
        self.add_project()
        self.make_git_root()
        self.add_ticket()
        self.add_attempt()
        code, out, err = self.run_tool()
        self.assertEqual(0, code, err)
        lines = self.lines(out)
        for label, line in zip(SKELETON_LABELS, lines[:5]):
            self.assertTrue(line.startswith(label), f"缺「{label}」这一行：{line}")
        self.assertIn(OWNER, lines[1])
        self.assertIn(f"python tools/receipt_payload.py --ticket {TICKET}", lines[4])
        self.assertIn("git worktree add", lines[3])
        self.assertIn(TICKET, lines[0])
        self.assertIn(self.shown_root(), lines[0])

        # 分隔线之后就是现场段原文：一字不改。
        self.assertIn(SEPARATOR, lines)
        tail = out.split(SEPARATOR, 1)[1].strip("\n")
        self.assertEqual(GOOD_CONTEXT.rstrip("\n"), tail)

    def test_context_keeps_its_own_lines_and_order(self):
        """现场段不被重排、不被润色：每一节的正文都在，顺序也照文件。"""
        self.add_project()
        self.make_git_root()
        self.add_ticket()
        self.add_attempt()
        _, out, _ = self.run_tool()
        for sentence in ("上一轮把 icon 的位置改了又改",
                         "`receipt_payload` 与 `record_notice` 的参数与语义一个字不动",
                         "骨架段五行 + 现场段文件校验"):
            self.assertIn(sentence, out)

    def test_no_expiring_values_in_the_skeleton(self):
        """骨架只写指针——attempt id 不进正文（它几小时内就会过期）。"""
        self.add_project()
        self.make_git_root()
        self.add_ticket()
        self.add_attempt()
        _, out, _ = self.run_tool()
        skeleton = out.split(SEPARATOR)[0]
        self.assertNotIn(ATTEMPT, skeleton)

    def test_json_output_is_machine_readable(self):
        """`--json` 给机器读：骨架与现场段分开给，字段名与文本一致。"""
        self.add_project()
        self.make_git_root()
        self.add_ticket()
        self.add_attempt()
        code, out, err = self.run_tool("--json")
        self.assertEqual(0, code, err)
        payload = json.loads(out)
        self.assertEqual(TICKET, payload["ticket"])
        self.assertEqual(OWNER, payload["owner_session_id"])
        self.assertEqual("git", payload["repo_kind"])
        self.assertEqual(2, payload["criteria_version"])
        self.assertIn(self.shown_root(), payload["skeleton"][0])
        self.assertIn("上一轮把 icon", payload["sections"]["why"])
        self.assertIn("最容易做歪", payload["lines"][-1])


class ContextChecks(Fixture):
    """现场段有问题时：非零、stdout 为空、stderr 点名缺哪一节。"""

    def assert_refused(self, code, out, err, *needles):
        self.assertEqual(2, code, err)
        self.assertEqual("", out, "拒绝时 stdout 一个字节都不许有")
        for needle in needles:
            self.assertIn(needle, err)

    def test_missing_context_argument_is_refused(self):
        self.add_project()
        self.make_git_root()
        self.add_ticket()
        self.add_attempt()
        code, out, err = self.run_tool(context=False)
        self.assert_refused(code, out, err, "--context")

    def test_missing_context_file_is_refused(self):
        self.add_project()
        self.make_git_root()
        self.add_ticket()
        self.add_attempt()
        self.context_path.unlink()
        code, out, err = self.run_tool()
        self.assert_refused(code, out, err, "不存在")

    def test_empty_context_file_is_refused(self):
        self.add_project()
        self.make_git_root()
        self.add_ticket()
        self.add_attempt()
        self.write_context("   \n\n")
        code, out, err = self.run_tool()
        self.assert_refused(code, out, err, "空")

    def test_missing_one_ring_is_refused_and_named(self):
        """C2：缺 How 一节就点名 How，别的字都不打。"""
        self.add_project()
        self.make_git_root()
        self.add_ticket()
        self.add_attempt()
        self.write_context("Why：不这么做会漏回执。\nWhat：骨架段与校验。\n最容易做歪的地方：\n1. 又抄一遍散文。\n")
        code, out, err = self.run_tool()
        self.assert_refused(code, out, err, "How")

    def test_empty_ring_body_is_refused_and_named(self):
        self.add_project()
        self.make_git_root()
        self.add_ticket()
        self.add_attempt()
        self.write_context("Why：不这么做会漏回执。\nHow：\nWhat：骨架段与校验。\n最容易做歪的地方：\n1. 又抄一遍散文。\n")
        code, out, err = self.run_tool()
        self.assert_refused(code, out, err, "How", "正文为空")

    def test_missing_pitfall_section_is_refused(self):
        self.add_project()
        self.make_git_root()
        self.add_ticket()
        self.add_attempt()
        self.write_context("Why：不这么做会漏回执。\nHow：只改工具与文档。\nWhat：骨架段与校验。\n")
        code, out, err = self.run_tool()
        self.assert_refused(code, out, err, "最容易做歪的地方")

    def test_why_as_pure_pointer_is_refused(self):
        """C2：「见 PRD」这种 Why 等于没写——机器判得出「一句自己的话都没有」。"""
        self.add_project()
        self.make_git_root()
        self.add_ticket()
        self.add_attempt()
        self.write_context("Why：见 PRD\nHow：只改工具与文档。\nWhat：骨架段与校验。\n最容易做歪的地方：\n1. 又抄一遍散文。\n")
        code, out, err = self.run_tool()
        self.assert_refused(code, out, err, "Why", "指针")

    def test_why_with_a_real_sentence_passes(self):
        """不是纯指针就放行：机器不判写得好不好，那是验收会话的事。"""
        self.add_project()
        self.make_git_root()
        self.add_ticket()
        self.add_attempt()
        self.write_context("Why：见 PRD 之后我判断回执这条路还会漏，所以先把消息来源收成一条命令。\n"
                           "How：只改工具与文档。\nWhat：骨架段与校验。\n最容易做歪的地方：\n1. 又抄一遍散文。\n")
        code, out, err = self.run_tool()
        self.assertEqual(0, code, err)


class Refusals(Fixture):
    def test_missing_owner_refuses_and_prints_nothing(self):
        """C1：没有 owner 就非零、stdout 为空——半条消息比没有更贵。"""
        self.add_project()
        self.make_git_root()
        self.add_ticket(owner=None)
        self.add_attempt()
        code, out, err = self.run_tool()
        self.assertEqual(2, code)
        self.assertEqual("", out, "拒绝时 stdout 一个字节都不许有")
        self.assertIn("owner", err)
        self.assertIn(TICKET, err)

    def test_ticket_without_attempt_is_refused(self):
        self.add_project()
        self.make_git_root()
        self.add_ticket(current_attempt=None)
        code, out, err = self.run_tool()
        self.assertEqual(2, code)
        self.assertEqual("", out)
        self.assertIn("claim_ticket", err)

    def test_unexpected_state_is_refused(self):
        """只有 dispatching / running 是「可以派出去」的两个状态。"""
        self.add_project()
        self.make_git_root()
        self.add_ticket(state="awaiting_review")
        self.add_attempt()
        code, out, err = self.run_tool()
        self.assertEqual(2, code)
        self.assertEqual("", out)
        self.assertIn("awaiting_review", err)

    def test_unknown_project_is_refused(self):
        """项目没登记就取不到项目根——不许猜一个目录出来。"""
        self.make_git_root()
        self.add_ticket()
        self.add_attempt()
        code, out, err = self.run_tool()
        self.assertEqual(2, code)
        self.assertEqual("", out)
        self.assertIn(PROJECT, err)


class NonGit(Fixture):
    def test_non_git_project_root_says_repo_kind(self):
        """C1：非 git 项目根要给 `repo_kind=non_git`，不能给一条会失败的工作树命令。"""
        self.add_project()
        self.add_ticket()
        self.add_attempt()
        code, out, err = self.run_tool()
        self.assertEqual(0, code, err)
        self.assertIn("repo_kind=non_git", out)
        self.assertNotIn("git worktree add", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
