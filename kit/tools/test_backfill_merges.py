"""tools/backfill_merges.py 自己的用例：能验证的补、不能验证的豁免、不落地的拒绝。

每条用例都断言**库里到底多了什么**，不看工具自己说成功了多少张：

1. `--dry-run` 打印 A/B/C 三档与每张单的判据，库与豁免文件一个字都不动；
2. `--apply` 只给 A 档写 `merges` 行，B 档一行不写，C 档拒绝并在输出里点名；
3. A 档原本没有回执时先补一条 `failed`（不是 `sent`），已有回执时不重复补；
4. 补记行带 `source=backfill`、`verified_by`（含 `merge-base --is-ancestor` 的可重跑命令）、
   `verified_at` 与 `note`；
5. `--apply` 把 B 档落成豁免清单（JSON + Markdown），没有 `head_sha` 是它唯一的理由；
6. C 档存在时 `--apply` 仍只写 A 档，但以非零退出，让「确实没落地」不能被当成成功。
"""
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import backfill_merges  # noqa: E402
from task_center import Center  # noqa: E402

HEAD_LANDED = "1" * 40
HEAD_SIDE = "2" * 40


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "项目一"
        self.project.mkdir()
        (self.project / "PRD.md").write_text("需求第一版", encoding="utf-8")
        self.git("init", "-b", "master", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "test")
        (self.project / "a.txt").write_text("a\n", encoding="utf-8")
        self.git("add", "a.txt")
        self.git("commit", "-q", "-m", "base")

        self.db = self.root / "center.sqlite3"
        self.center = Center(self.db)
        self.counter = 0
        self.ok("init")
        self.ok("register_project", {"project_id": "p1", "name": "项目一", "root": str(self.project)})
        self.exemptions_json = self.root / "exemptions.json"
        self.exemptions_md = self.root / "exemptions.md"
        self.ids = {}

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.project), *args], capture_output=True,
                              text=True, check=True).stdout.strip()

    def call(self, op, payload=None):
        self.counter += 1
        return self.center.execute(op, payload or {}, f"req-{self.counter}")

    def ok(self, op, payload=None):
        result = self.call(op, payload)
        self.assertTrue(result["ok"], result)
        return result["result"]

    def landed_sha(self):
        (self.project / "b.txt").write_text("b\n", encoding="utf-8")
        self.git("add", "b.txt")
        self.git("commit", "-q", "-m", "landed")
        return self.git("rev-parse", "HEAD")

    def side_sha(self):
        self.git("switch", "-q", "-c", "side")
        (self.project / "c.txt").write_text("c\n", encoding="utf-8")
        self.git("add", "c.txt")
        self.git("commit", "-q", "-m", "side")
        sha = self.git("rev-parse", "HEAD")
        self.git("switch", "-q", "master")
        return sha

    def done(self, alias, head_sha):
        """走完整流程做出一张 done 单；`head_sha` 直接决定它落在哪一档。"""
        created = self.ok("create_ticket", {
            "project_id": "p1", "title": f"历史单 {alias}",
            "deliverable": "一份清单", "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        })
        tid = created["id"]
        self.ids[alias] = tid
        scope = {"project_id": "p1", "ticket_id": tid}
        claim = self.ok("claim_ticket", {**scope, "expected_revision": 1,
                                        "coordinator_session_id": "coordinator-1"})
        self.ok("bind_session", {**scope, "attempt_id": claim["attempt_id"],
                                 "session_id": f"executor-{alias}", "project_root": str(self.project)})
        (self.project / f"{alias}.md").write_text("清单", encoding="utf-8")
        submission = self.ok("submit_result", {
            **scope, "attempt_id": claim["attempt_id"], "session_id": f"executor-{alias}",
            "artifacts": [f"{alias}.md"], "summary": "完成", "evidence": ["核对过"],
            "risks": [], "head_sha": head_sha, "base_sha": "89abcdef0123456789abcdef0123456789abcdef",
        })
        self.ok("review_result", {
            **scope, "attempt_id": claim["attempt_id"], "submission_id": submission["submission_id"],
            "reviewer_session_id": f"reviewer-{alias}", "verdict": "accept",
            "checks": [{"id": "C1", "passed": True, "evidence": "已核对"}], "reason": "通过",
        })
        return tid

    def forget_head_sha(self, alias):
        """把提交改成 schema 9 以前的样子：没有 head_sha 可核对。"""
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE submissions SET head_sha=NULL WHERE attempt_id="
                         "(SELECT id FROM attempts WHERE ticket_id=?)", (self.ids[alias],))
            conn.commit()
        finally:
            conn.close()

    def run_tool(self, dry_run):
        chunks = []
        code = backfill_merges.run(self.center, dry_run=dry_run,
                                   exemptions_json=self.exemptions_json,
                                   exemptions_md=self.exemptions_md, out=chunks.append)
        return code, "".join(chunks)

    def ticket(self, alias):
        return self.ok("get_ticket", {"project_id": "p1", "ticket_id": self.ids[alias]})

    def merge_count(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM merges").fetchone()[0]
        finally:
            conn.close()

    def notice_rows(self, alias):
        return self.ticket(alias)["notices"]

    def build_three_buckets(self):
        landed = self.landed_sha()
        side = self.side_sha()
        self.done("A", landed)
        self.done("B", HEAD_LANDED)
        self.forget_head_sha("B")
        self.done("C", side)

    def test_dry_run_prints_three_buckets_and_writes_nothing(self):
        self.build_three_buckets()
        code, text = self.run_tool(dry_run=True)
        self.assertEqual(0, code, text)
        self.assertIn("A 档", text)
        self.assertIn("B 档", text)
        self.assertIn("C 档", text)
        self.assertIn("A=1", text)
        self.assertIn("B=1", text)
        self.assertIn("C=1", text)
        # 每种档位都要能说清是哪张单：只给张数等于让人自己去猜。
        self.assertIn(self.ids["A"], text)
        self.assertIn(self.ids["B"], text)
        self.assertIn(self.ids["C"], text)
        self.assertIn("merge-base --is-ancestor", text)
        # 预演不写库、不落豁免文件。
        self.assertEqual(0, self.merge_count())
        self.assertFalse(self.exemptions_json.exists())
        self.assertFalse(self.exemptions_md.exists())

    def test_apply_backfills_only_the_verifiable_bucket(self):
        self.build_three_buckets()
        code, text = self.run_tool(dry_run=False)
        # C 档存在：A 档照写，但整体非零，不能把「有单没落地」报成成功。
        self.assertEqual(2, code, text)
        merges = self.ticket("A")["merges"]
        self.assertEqual(1, len(merges))
        merge = merges[0]
        self.assertEqual("backfill", merge["source"])
        self.assertIsNotNone(merge["verified_at"])
        self.assertIn("merge-base --is-ancestor", merge["verified_by"])
        self.assertIn("补记", merge["note"])
        # B / C 一行都不写。
        self.assertEqual([], self.ticket("B")["merges"])
        self.assertEqual([], self.ticket("C")["merges"])
        self.assertEqual(1, self.merge_count())
        self.assertIn("NOT_LANDED", text)
        self.assertIn(self.ids["C"], text)

    def test_apply_adds_a_failed_notice_when_there_was_none(self):
        self.build_three_buckets()
        self.assertEqual([], self.notice_rows("A"))
        self.run_tool(dry_run=False)
        notices = self.notice_rows("A")
        self.assertEqual(1, len(notices))
        self.assertEqual("failed", notices[0]["status"])
        self.assertIn("补记", notices[0]["detail"])
        self.assertIn("还没有回执机制", notices[0]["detail"])

    def test_apply_keeps_an_existing_notice_instead_of_adding_another(self):
        self.build_three_buckets()
        ticket = self.ticket("A")
        self.ok("record_notice", {
            "project_id": "p1", "ticket_id": self.ids["A"],
            "attempt_id": ticket["attempts"][-1]["id"], "session_id": "coordinator-1",
            "origin_session_id": "coordinator-1", "status": "failed",
            "detail": "当年的回执记录", "target_session_id": "coordinator-1", "wake_kind": "queued",
        })
        self.run_tool(dry_run=False)
        notices = self.notice_rows("A")
        self.assertEqual(1, len(notices))
        self.assertEqual("当年的回执记录", notices[0]["detail"])

    def test_apply_writes_the_exemption_list_for_the_unverifiable_bucket(self):
        self.build_three_buckets()
        self.run_tool(dry_run=False)
        data = json.loads(self.exemptions_json.read_text(encoding="utf-8"))
        entries = {entry["ticket_id"]: entry for entry in data["entries"]}
        self.assertEqual({self.ids["B"]}, set(entries))
        self.assertEqual("no_head_sha", entries[self.ids["B"]]["reason"])
        self.assertEqual("p1", entries[self.ids["B"]]["project_id"])
        markdown = self.exemptions_md.read_text(encoding="utf-8")
        self.assertIn(self.ids["B"], markdown)
        self.assertIn("no_head_sha", markdown)
        # A 档已经有记录了，不该被算进豁免；C 档没落地，也不能被「豁免」掉。
        self.assertNotIn(self.ids["A"], entries)
        self.assertNotIn(self.ids["C"], entries)

    def test_apply_is_idempotent(self):
        self.build_three_buckets()
        self.run_tool(dry_run=False)
        first = self.merge_count()
        self.run_tool(dry_run=False)
        self.assertEqual(first, self.merge_count())
        self.assertEqual(1, len(self.notice_rows("A")))

    def test_empty_database_says_so(self):
        code, text = self.run_tool(dry_run=True)
        self.assertEqual(0, code, text)
        self.assertIn("没有", text)

    def test_apply_writes_an_empty_list_when_there_is_nothing_to_exempt(self):
        """AW-130：一张待豁免的单都没有时也要落一份空清单，不留旧快照冒充现状。

        以前 `--apply` 在「待补记集合为空」时什么都不写，于是 2026-11-22 那份列着
        51 张的快照一直留在仓库里，而那 51 张早就被 `archived` 终态覆盖了。
        """
        self.exemptions_json.write_text(json.dumps({"entries": [
            {"ticket_id": "AW-004", "project_id": "p1", "reason": "no_head_sha",
             "reason_text": "旧快照里的一行"},
        ]}, ensure_ascii=False), encoding="utf-8")
        code, text = self.run_tool(dry_run=False)
        self.assertEqual(0, code, text)
        data = json.loads(self.exemptions_json.read_text(encoding="utf-8"))
        self.assertEqual([], data["entries"])
        self.assertIn("archived", data["empty_means"])
        markdown = self.exemptions_md.read_text(encoding="utf-8")
        self.assertIn("本次豁免 0 张", markdown)
        self.assertIn("archived", markdown)
        # 旧快照里的名字不许留着：留着读的人就会以为它还在等账。
        self.assertNotIn("AW-004", markdown)
        self.assertEqual(0, self.merge_count())


if __name__ == "__main__":
    unittest.main()
