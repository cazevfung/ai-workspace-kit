"""`tools/archive_legacy.py` 的判据：谁该被归档、谁不该、dry-run 一行不写。

这张单的三条风险都在判据里，不在命令形状里：

1. **日期界**：界之后的单缺记录是「流程没走完」（该补回执、该走合并），不是历史断链。
   判错会把今天刚验收、正在等合并的单一起归档——那正是 C9 的反例。
2. **两类缺口各自算**：只缺回执、只缺合并、两类都缺都要进；两类都不缺（正常闭环过的单）
   不许进，否则「归档」就变成「把所有 done 都扫成历史」。
3. **dry-run 只读**：`--dry-run` 跑完库的字节不变、journal 目录不新建。

日期界是 `2026-11-21T00:00:00+00:00`，用例里不重抄这个值，从模块常量取。
"""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import archive_legacy  # noqa: E402
from task_center import Center  # noqa: E402

PROJECT, TICKET = "p1", "AW-900"


def legacy_db(path: Path, rows) -> None:
    """只建 `archive_legacy.legacy_rows()` 读得到的那几张表，往里面灌给定的行。

    这里直接用 SQL 建表是允许的：被验的是「读出来的判据对不对」，不是写入路径——
    写入路径（`archive_ticket`）在 `tests/test_archive.py` 里走真流程。
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tickets(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
         state TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE notices(id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL);
        CREATE TABLE merges(id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL);
        """
    )
    for row in rows:
        conn.execute("INSERT INTO tickets VALUES (?,?,?,?,?)",
                     (row["id"], PROJECT, row.get("title", row["id"]), row["state"], row["created_at"]))
        if row.get("notices", 0):
            for index in range(row["notices"]):
                conn.execute("INSERT INTO notices VALUES (?,?)", (f"{row['id']}-n{index}", row["id"]))
        if row.get("merges", 0):
            for index in range(row["merges"]):
                conn.execute("INSERT INTO merges VALUES (?,?)", (f"{row['id']}-m{index}", row["id"]))
    conn.commit()
    conn.close()


class LegacyRowsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "center.sqlite3"

    def rows(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return archive_legacy.legacy_rows(conn)
        finally:
            conn.close()

    def test_only_the_four_kinds_of_legacy_rows_are_picked(self):
        legacy_db(self.db, [
            # 本该进：11-21 前的 done，缺回执 / 缺合并 / 两类都缺。
            {"id": "AW-001", "state": "done", "created_at": "2026-11-20T23:59:59+00:00"},
            {"id": "AW-002", "state": "done", "created_at": "2026-11-10T00:00:00+00:00",
             "notices": 1},
            {"id": "AW-003", "state": "done", "created_at": "2026-11-01T00:00:00+00:00",
             "merges": 1},
            # 不该进：闭环过的单（回执与合并都有）。
            {"id": "AW-004", "state": "done", "created_at": "2026-11-02T00:00:00+00:00",
             "notices": 1, "merges": 1},
            # 不该进：状态不是 done。
            {"id": "AW-005", "state": "running", "created_at": "2026-11-02T00:00:00+00:00"},
            {"id": "AW-006", "state": "cancelled", "created_at": "2026-11-02T00:00:00+00:00"},
        ])
        picked = {row["ticket_id"]: row for row in self.rows()}
        self.assertEqual({"AW-001", "AW-002", "AW-003"}, set(picked))
        self.assertEqual([archive_legacy.GAP_NOTICE, archive_legacy.GAP_MERGE], picked["AW-001"]["gaps"])
        self.assertEqual([archive_legacy.GAP_MERGE], picked["AW-002"]["gaps"])
        self.assertEqual([archive_legacy.GAP_NOTICE], picked["AW-003"]["gaps"])

    def test_the_date_boundary_is_strict(self):
        legacy_db(self.db, [
            {"id": "AW-001", "state": "done", "created_at": "2026-11-21T00:00:00+00:00"},
            {"id": "AW-002", "state": "done", "created_at": "2026-11-20T23:59:59.999999+00:00"},
        ])
        picked = [row["ticket_id"] for row in self.rows()]
        # 界那一刻的自己不算「早于界」：`created_at < cutoff`，不是 `<=`。
        self.assertEqual(["AW-002"], picked)

    def test_a_ticket_created_today_is_not_legacy(self):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).isoformat()
        legacy_db(self.db, [{"id": "AW-777", "state": "done", "created_at": today}])
        self.assertEqual([], self.rows())


class ApplyTest(unittest.TestCase):
    """`--apply` 的真库演练：只在这里验「归档写进的是 archived 状态 + 留一条事件 + journal」。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "项目"
        self.project.mkdir()
        (self.project / "PRD.md").write_text("需求", encoding="utf-8")
        self.db = self.root / "center.sqlite3"
        self.center = Center(self.db)
        self.center.execute("init", {}, request_id="t-init")
        self.center.execute("register_project", {"project_id": PROJECT, "name": "项目",
                                                 "root": str(self.project)}, request_id="t-reg")
        self.journal = self.root / "journal"

    def done_ticket(self, created_at=None):
        """造一张 `done` 的单。直接改 `state` 与 `created_at` 是夹具手法（两者都不是任何操作能设的），
        被验的是工具读到这种行之后的判定与写入，不是工单怎么走到 done。"""
        created = self.center.execute("create_ticket", {
            "project_id": PROJECT, "title": "老单", "deliverable": "历史事实",
            "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        }, request_id=f"t-create-{created_at}")
        ticket_id = created["result"]["id"]
        conn = sqlite3.connect(self.db)
        if created_at:
            conn.execute("UPDATE tickets SET state='done', created_at=? WHERE id=?", (created_at, ticket_id))
        else:
            conn.execute("UPDATE tickets SET state='done' WHERE id=?", (ticket_id,))
        conn.commit()
        conn.close()
        return ticket_id

    def run_tool(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "tools" / "archive_legacy.py"),
                               "--db", str(self.db), "--journal-dir", str(self.journal), *args],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")

    def test_dry_run_writes_nothing_and_apply_leaves_a_journal(self):
        old = self.done_ticket("2026-11-10T00:00:00+00:00")
        fresh = self.done_ticket()  # 今天创建：界之后，不该被归档

        before = self.db.read_bytes()
        dry = self.run_tool("--dry-run")
        self.assertEqual(0, dry.returncode, dry.stderr)
        self.assertIn("合计：1 张候选", dry.stdout)
        self.assertEqual(before, self.db.read_bytes(), "dry-run 不许写库")
        self.assertFalse(self.journal.exists(), "dry-run 不许建 journal 目录")

        applied = self.run_tool("--apply")
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertIn("归档完成：1 张", applied.stdout)
        conn = sqlite3.connect(self.db)
        states = dict(conn.execute("SELECT id, state FROM tickets").fetchall())
        events = conn.execute("SELECT COUNT(*) FROM events WHERE operation='archive_ticket'").fetchone()[0]
        conn.close()
        self.assertEqual("archived", states[old])
        self.assertEqual("done", states[fresh])
        self.assertEqual(1, events)

        files = sorted(self.journal.glob("archive-legacy-*.json"))
        self.assertEqual(1, len(files))
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual([old], payload["ids"])
        self.assertEqual(1, payload["count"])
        self.assertEqual([], payload["failures"])

        # 再跑一次：候选为 0，且不写一份空账把「最新那份」变成零。
        again = self.run_tool("--apply")
        self.assertEqual(0, again.returncode, again.stderr)
        self.assertIn("合计：0 张将归档", again.stdout)
        self.assertEqual(1, len(sorted(self.journal.glob("archive-legacy-*.json"))))


if __name__ == "__main__":
    unittest.main()
