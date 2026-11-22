"""schema 13：提交时冲突预演（PRD-015 R2）。

`submit_result` 在提交那一刻对未来目标分支跑一次
`git merge-tree --write-tree --name-only <head_sha> <目标分支 tip>`，把「这份成果将来合进
现在的 master 会撞哪些文件」写进回执与库里的 `conflict_preview`。

三种取值必须分得开：`conflicts` 是空数组＝跑过且干净；`conflicts` 非空、`exit_code` 是 1
＝跑过且会撞；整个 `conflict_preview` 是 `null` ＝算不出来（不是仓库、head 不在本地、git
不可用），不是「没撞」。把最后一种写成空值，等于说「预演过了、没冲突」，那是一句假话。

预演只报告、不处置：撞了也照收提交（`ok` 仍是 true），不回滚、不自动解冲突、不改分支。
提交那一刻撞不撞不等于最终撞不撞——分支还要 rebase、重订，另一份提交可能就干净了；
把它做成闸门只会教人先 rebase 一次再提交，回执里就再也看不到真相（PRD-015 §4 R2）。
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from task_center import Center

CRITERIA = [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}]


class CenterBase(unittest.TestCase):
    """一个临时项目 + 一个中心库；仓库是否 init 由各用例自己决定。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "中文项目"
        self.project.mkdir()
        (self.project / "PRD.md").write_text("需求第一版\n", encoding="utf-8")
        self.db = self.root / "center.sqlite3"
        self.center = Center(self.db)
        self.counter = 0
        self.ok("init")
        self.ok("register_project", {"project_id": "p1", "name": "项目一", "root": str(self.project)})

    def call(self, op, payload=None):
        self.counter += 1
        return self.center.execute(op, payload or {}, f"req-{self.counter}")

    def ok(self, op, payload=None):
        result = self.call(op, payload)
        self.assertTrue(result["ok"], result)
        return result["result"]

    def ticket(self):
        return self.ok("create_ticket", {
            "project_id": "p1", "title": "改一处说明", "deliverable": "改好的说明",
            "prd_path": "PRD.md", "criteria": CRITERIA})["id"]

    def running(self, ticket_id):
        claim = self.ok("claim_ticket", {"project_id": "p1", "ticket_id": ticket_id,
                                         "expected_revision": 1,
                                         "coordinator_session_id": "coordinator-1"})
        self.ok("bind_session", {"project_id": "p1", "ticket_id": ticket_id,
                                 "attempt_id": claim["attempt_id"], "session_id": "executor-1",
                                 "project_root": str(self.project)})
        return claim["attempt_id"]

    def submit(self, ticket_id, attempt, head_sha, base_sha, artifact="result.md"):
        (self.project / artifact).write_text("1. 检查步骤\n", encoding="utf-8")
        return self.ok("submit_result", {
            "project_id": "p1", "ticket_id": ticket_id, "attempt_id": attempt,
            "session_id": "executor-1", "artifacts": [artifact], "summary": "改完了",
            "evidence": ["人工核对"], "risks": [],
            "head_sha": head_sha, "base_sha": base_sha})


@unittest.skipUnless(shutil.which("git"), "需要 git 才能预演冲突")
class ConflictPreviewTest(CenterBase):
    """R2 的三种取值：会撞、干净、算不出来。"""

    def git(self, *args, expect=0):
        process = subprocess.run(["git", "-C", str(self.project), *args], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(expect, process.returncode, process.stderr)
        return process.stdout.strip()

    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        # 行尾转换会让 checkout 改写工作区的 PRD.md，指纹一变就是 STALE_PRD——
        # 那条闸门验的是需求有没有变，不该被 CRLF 触发，所以这里把转换关掉。
        self.git("config", "core.autocrlf", "false")
        self.git("config", "core.eol", "lf")
        self.git("config", "user.email", "executor@example.com")
        self.git("config", "user.name", "executor")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "第一版需求")
        self.git("branch", "-M", "master")
        self.base = self.git("rev-parse", "HEAD")

    def branch_edit(self, branch, text):
        """从 master 开一条分支，把 note.md 第一行改掉并提交，返回那条提交的 sha。"""
        self.git("checkout", "-q", "-b", branch, "master")
        (self.project / "note.md").write_text(text, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"{branch} 改了这一行")
        head = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "master")
        return head

    def test_submit_reports_merge_conflicts(self):
        """C3 / AC-R2-1：两边改同一行时，回执与库里都列出那个文件，且提交照收。"""
        ticket_id = self.ticket()
        attempt = self.running(ticket_id)
        head = self.branch_edit("ticket/t1", "成果改的这一版\n")
        (self.project / "note.md").write_text("master 后来的这一版\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "master 也改了同一行")
        target_sha = self.git("rev-parse", "master")

        receipt = self.submit(ticket_id, attempt, head, self.base)

        preview = receipt["conflict_preview"]
        self.assertEqual(["note.md"], preview["conflicts"])
        self.assertEqual(1, preview["exit_code"])
        self.assertEqual("master", preview["target_branch"])
        self.assertEqual(target_sha, preview["target_sha"])
        self.assertIn(head, preview["command"])
        # 只报告不处置：撞了也照收，工单进 awaiting_review 而不是被拒。
        self.assertEqual("awaiting_review", receipt["state"])
        ticket = self.ok("get_ticket", {"project_id": "p1", "ticket_id": ticket_id})
        self.assertEqual("awaiting_review", ticket["state"])
        self.assertEqual(preview, ticket["submissions"][-1]["conflict_preview"])

    def test_submit_clean_merge_field_empty(self):
        """C6 / AC-R2-2：干净时字段照常出现，`conflicts` 是空数组而不是缺席或 null。"""
        ticket_id = self.ticket()
        attempt = self.running(ticket_id)
        head = self.branch_edit("ticket/t1", "原始一行\n")
        target_sha = self.git("rev-parse", "master")

        receipt = self.submit(ticket_id, attempt, head, self.base)

        # 字段存在而不是省略：把整份回执序列化后它只出现一次（AC-R2-2 的计数口径）。
        self.assertEqual(1, json.dumps(receipt, ensure_ascii=False).count('"conflict_preview"'))
        preview = receipt["conflict_preview"]
        self.assertIsNotNone(preview)
        self.assertEqual([], preview["conflicts"])
        self.assertEqual(0, preview["exit_code"])
        self.assertEqual("master", preview["target_branch"])
        self.assertEqual(target_sha, preview["target_sha"])
        ticket = self.ok("get_ticket", {"project_id": "p1", "ticket_id": ticket_id})
        self.assertEqual(preview, ticket["submissions"][-1]["conflict_preview"])

    def test_conflicts_are_sorted_and_deduplicated(self):
        """路径数组按字母序去重：验收会话要能逐条比对，不靠输出顺序。"""
        ticket_id = self.ticket()
        attempt = self.running(ticket_id)
        for name in ("b.md", "a.md"):
            (self.project / name).write_text("原始\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "加两个文件")
        base = self.git("rev-parse", "HEAD")

        self.git("checkout", "-q", "-b", "ticket/t1", "master")
        for name in ("b.md", "a.md"):
            (self.project / name).write_text("成果这一版\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "成果改了两个文件")
        head = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "master")
        for name in ("b.md", "a.md"):
            (self.project / name).write_text("master 这一版\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "master 改了同两个文件")

        preview = self.submit(ticket_id, attempt, head, base)["conflict_preview"]
        self.assertEqual(["a.md", "b.md"], preview["conflicts"])


@unittest.skipUnless(shutil.which("git"), "需要 git 才能预演冲突")
class PreviewNotComputableTest(CenterBase):
    """算不出来时是 null，不是空数组：两者在验收会话眼里是两件事（PRD-015 §4 R2）。"""

    def test_non_repository_directory_leaves_preview_null(self):
        ticket_id = self.ticket()
        attempt = self.running(ticket_id)
        receipt = self.submit(ticket_id, attempt,
                              "0123456789abcdef0123456789abcdef01234567",
                              "89abcdef0123456789abcdef0123456789abcdef")
        self.assertIn("conflict_preview", receipt)
        self.assertIsNone(receipt["conflict_preview"])
        # 提交没有被这一项拖住：算不出来不等于提交失败。
        self.assertEqual("awaiting_review", receipt["state"])
        ticket = self.ok("get_ticket", {"project_id": "p1", "ticket_id": ticket_id})
        self.assertIn("conflict_preview", ticket["submissions"][-1])
        self.assertIsNone(ticket["submissions"][-1]["conflict_preview"])

    def test_unknown_head_sha_leaves_preview_null(self):
        """head 不在本地＝算不出来。git 会自己拒绝，回执留 null 而不是编一份空数组。"""
        project = self.project
        subprocess.run(["git", "-C", str(project), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(project), "config", "core.autocrlf", "false"], check=True)
        subprocess.run(["git", "-C", str(project), "config", "user.email", "e@example.com"], check=True)
        subprocess.run(["git", "-C", str(project), "config", "user.name", "e"], check=True)
        subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(project), "commit", "-q", "-m", "第一版"], check=True)
        subprocess.run(["git", "-C", str(project), "branch", "-M", "master"], check=True)
        ticket_id = self.ticket()
        attempt = self.running(ticket_id)
        receipt = self.submit(ticket_id, attempt,
                              "fedcba9876543210fedcba9876543210fedcba98",
                              "89abcdef0123456789abcdef0123456789abcdef")
        self.assertIsNone(receipt["conflict_preview"])


if __name__ == "__main__":
    unittest.main()
