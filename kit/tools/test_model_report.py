"""模型对照表：口径是否真的落到聚合结果上。

这组用例锁的是「口径」，不是「脚本能跑」：一张单被 review 多次要算返工、
但花费不能跟着被算多遍；只有一次 accept 才算一次通过；模型按会话去重后再求和。
用两个临时 sqlite 当 fixture（表结构与真库一致的最小集合），
不读真实库、不依赖真实工单号——否则口径一改，用例跟着真库波动，等于没锁。
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import model_report

# 只建脚本真的会读的那几张表与那几列；列名必须与真库一致，否则这组用例
# 锁的就不是线上口径。
CENTER_SCHEMA = """
create table tickets(
  id text primary key, project_id text not null, title text not null, deliverable text not null,
  prd_path text not null, prd_sha256 text not null, criteria text not null,
  revision integer not null, state text not null, current_attempt text, created_at text not null);
create table attempts(
  id text primary key, ticket_id text not null, revision integer not null,
  prd_path text not null, prd_sha256 text not null, state text not null,
  session_id text, created_at text not null, coordinator_session_id text);
create table submissions(
  id text primary key, attempt_id text not null unique, summary text not null,
  artifacts text not null, evidence text not null, risks text not null, created_at text not null);
create table reviews(
  id text primary key, submission_id text not null unique, reviewer_session_id text not null,
  verdict text not null, checks text not null, reason text not null, created_at text not null);
"""

CINDY_SCHEMA = """
create table sessions(
  id text primary key, model text not null, effort text not null,
  total_token_usage integer not null default 0, total_cost_amount real not null default 0,
  total_cost_currency text, created_at integer not null, last_activity_at integer);
"""

MINUTE = 60_000


class ModelReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.center_path = self.root / "center.sqlite3"
        self.cindy_path = self.root / "cindy-test.db"
        self.center = self._create(self.center_path, CENTER_SCHEMA)
        self.cindy = self._create(self.cindy_path, CINDY_SCHEMA)
        # 注册在 tmp.cleanup 之后 → LIFO 先关连接，否则 Windows 上删不掉库文件
        self.addCleanup(self.close)
        self.counter = 0

    def _create(self, path, schema):
        conn = sqlite3.connect(path)
        conn.executescript(schema)
        conn.commit()
        return conn

    # -- fixture ---------------------------------------------------------
    def ticket(self, ticket_id):
        self.counter += 1
        self.center.execute(
            "insert into tickets values (?,?,?,?,?,?,?,?,?,NULL,?)",
            (ticket_id, "demo", "样例", "交付物", "PRD/PRD-003.md", "sha", "[]", 1, "done",
             "2026-11-01T00:00:00"),
        )
        return ticket_id

    def attempt(self, ticket_id, session_id, attempt_id=None, minute=0):
        self.counter += 1
        attempt_id = attempt_id or f"att-{self.counter}"
        self.center.execute(
            "insert into attempts values (?,?,?,?,?,?,?,?,?)",
            (attempt_id, ticket_id, 1, "PRD/PRD-003.md", "sha", "accepted", session_id,
             f"2026-11-01T00:{minute:02d}:00", None),
        )
        return attempt_id

    def review(self, attempt_id, verdict):
        self.counter += 1
        submission_id = f"sub-{self.counter}"
        self.center.execute(
            "insert into submissions values (?,?,?,?,?,?,?)",
            (submission_id, attempt_id, "摘要", "[]", "证据", "", f"2026-11-02T00:{self.counter:02d}:00"),
        )
        self.center.execute(
            "insert into reviews values (?,?,?,?,?,?,?)",
            (f"rev-{self.counter}", submission_id, "reviewer", verdict, "[]", "",
             f"2026-11-02T00:{self.counter:02d}:00"),
        )
        return submission_id

    def session(self, session_id, model="deepseek/deepseek-v4.1-flash", effort="high",
                tokens=1000, cost=1.5, currency="CNY", minutes=3.0):
        started = 1_760_000_000_000
        self.cindy.execute(
            "insert into sessions values (?,?,?,?,?,?,?,?)",
            (session_id, model, effort, tokens, cost, currency, started,
             started + int(minutes * MINUTE)),
        )
        return session_id

    def close(self):
        for conn in (getattr(self, "center", None), getattr(self, "cindy", None)):
            if conn is None:
                continue
            try:
                conn.commit()
                conn.close()
            except sqlite3.Error:
                pass

    def report(self, cindy_path=None):
        self.close()
        return model_report.build_report(self.center_path, cindy_path or self.cindy_path)

    def row(self, report, ticket_id):
        for row in report.rows:
            if row.ticket == ticket_id:
                return row
        self.fail(f"报告里没有 {ticket_id}")

    def bucket(self, report, model_key):
        for bucket in report.summary:
            if bucket["model"] == model_key:
                return bucket
        self.fail(f"汇总里没有 {model_key}")

    # -- 口径：一次通过 / 返工 --------------------------------------------
    def test_single_accept_is_one_pass(self):
        tid = self.ticket("T-1")
        self.session("s1")
        self.review(self.attempt(tid, "s1"), "accept")

        row = self.row(self.report(), tid)

        self.assertEqual(row.reviews, 1)
        self.assertEqual(row.attempts, 1)
        self.assertEqual(row.outcome, model_report.ONE_PASS)

    def test_multiple_reviews_is_rework_even_if_last_accepts(self):
        tid = self.ticket("T-2")
        self.session("s1")
        self.review(self.attempt(tid, "s1", "att-a"), "reject")
        self.review(self.attempt(tid, "s1", "att-b"), "accept")

        row = self.row(self.report(), tid)

        self.assertEqual(row.reviews, 2)
        self.assertEqual(row.outcome, model_report.REWORK)

    def test_last_review_reject_is_rework(self):
        tid = self.ticket("T-3")
        self.session("s1")
        self.session("s2")
        self.review(self.attempt(tid, "s1", "att-a"), "accept")
        self.review(self.attempt(tid, "s2", "att-b"), "reject")

        row = self.row(self.report(), tid)

        self.assertEqual(row.reviews, 2)
        self.assertEqual(row.verdicts, ("accept", "reject"))
        self.assertEqual(row.outcome, model_report.REWORK)

    def test_no_review_is_unreviewed_not_rework(self):
        """一次 review 都没有的单还没被判过，不该混进返工数里。"""
        tid = self.ticket("T-4")
        self.session("s1")
        self.attempt(tid, "s1")

        row = self.row(self.report(), tid)

        self.assertEqual(row.reviews, 0)
        self.assertEqual(row.outcome, model_report.UNREVIEWED)

    # -- 口径：花费 / token / 墙钟按会话去重 ------------------------------
    def test_repeated_attempts_do_not_multiply_cost(self):
        """同一张单五次尝试落在同一条会话上：花费只算一次。"""
        tid = self.ticket("T-5")
        self.session("s1", tokens=10_742_402, cost=1.2261067559999996, minutes=20.0)
        for index, verdict in enumerate(("reject", "reject", "reject", "accept")):
            self.review(self.attempt(tid, "s1", f"att-{index}"), verdict)
        self.attempt(tid, "s1", "att-last")

        row = self.row(self.report(), tid)

        self.assertEqual(row.attempts, 5)
        self.assertEqual(row.reviews, 4)
        self.assertAlmostEqual(row.cost["CNY"], 1.2261067559999996, places=12)
        self.assertEqual(row.tokens, 10_742_402)
        self.assertAlmostEqual(row.minutes, 20.0, places=6)
        self.assertEqual(row.outcome, model_report.REWORK)

    def test_distinct_sessions_are_summed(self):
        tid = self.ticket("T-6")
        self.session("s1", tokens=100, cost=1.0, minutes=2.5)
        self.session("s2", tokens=200, cost=2.0, minutes=7.5)
        self.review(self.attempt(tid, "s1", "att-a"), "reject")
        self.review(self.attempt(tid, "s2", "att-b"), "accept")

        row = self.row(self.report(), tid)

        self.assertAlmostEqual(row.cost["CNY"], 3.0, places=9)
        self.assertEqual(row.tokens, 300)
        self.assertAlmostEqual(row.minutes, 10.0, places=6)

    # -- 口径：模型列与汇总 ----------------------------------------------
    def test_models_joined_when_attempts_used_different_models(self):
        tid = self.ticket("T-7")
        self.session("s1", model="deepseek/deepseek-v4-pro")
        self.session("s2", model="deepseek/deepseek-v4.1-flash")
        self.review(self.attempt(tid, "s1", "att-a"), "reject")
        self.session("s3", model="deepseek/deepseek-v4-pro")
        self.review(self.attempt(tid, "s2", "att-b"), "accept")

        row = self.row(self.report(), tid)

        self.assertEqual(row.models, ("deepseek/deepseek-v4-pro", "deepseek/deepseek-v4.1-flash"))
        self.assertEqual(
            row.model_key, "deepseek/deepseek-v4-pro#deepseek/deepseek-v4.1-flash"
        )

    def test_summary_counts_tickets_by_model(self):
        one = self.ticket("T-8")
        self.session("s1", model="model-a")
        self.review(self.attempt(one, "s1"), "accept")

        other = self.ticket("T-9")
        self.session("s2", model="model-a")
        self.review(self.attempt(other, "s2", "att-b"), "reject")
        self.review(self.attempt(other, "s2", "att-c"), "accept")

        third = self.ticket("T-10")
        self.session("s3", model="model-b")
        self.review(self.attempt(third, "s3"), "accept")

        report = self.report()

        bucket_a = self.bucket(report, "model-a")
        self.assertEqual(bucket_a["tickets"], 2)
        self.assertEqual(bucket_a[model_report.ONE_PASS], 1)
        self.assertEqual(bucket_a[model_report.REWORK], 1)
        self.assertEqual(bucket_a[model_report.UNREVIEWED], 0)

        bucket_b = self.bucket(report, "model-b")
        self.assertEqual(bucket_b["tickets"], 1)
        self.assertEqual(bucket_b[model_report.ONE_PASS], 1)

        # 口工单数之和必须等于工单数：跨模型的单不会被数两次
        self.assertEqual(sum(b["tickets"] for b in report.summary), 3)

    def test_missing_session_is_reported_not_fatal(self):
        """会话在 Cindy 库里查不到（历史坏 id）时，报告要能出，并标明是缺失。"""
        tid = self.ticket("T-11")
        self.attempt(tid, "查不到的会话")
        self.session("s1")
        self.review(self.attempt(tid, "s1", "att-b"), "accept")

        row = self.row(self.report(), tid)

        self.assertEqual(row.attempts, 2)
        self.assertEqual(row.reviews, 1)
        self.assertEqual(row.missing_sessions, ("查不到的会话",))
        self.assertEqual(row.models, ("deepseek/deepseek-v4.1-flash",))

    # -- 只读与缺库 -------------------------------------------------------
    def test_missing_cindy_db_fails_without_creating_it(self):
        self.ticket("T-12")
        self.close()
        absent = self.root / "没有这个库.db"

        with self.assertRaises(model_report.ReportError) as caught:
            model_report.build_report(self.center_path, absent)

        self.assertIn("不存在", str(caught.exception))
        self.assertFalse(absent.exists(), "缺库时必须失败，不许顺手建一个空库")

    def test_report_does_not_touch_either_database(self):
        """只读：跑一趟前后两个库的字节、mtime、文件列表都不变。"""
        tid = self.ticket("T-13")
        self.session("s1")
        self.review(self.attempt(tid, "s1"), "accept")
        self.close()

        watched = [self.center_path, self.cindy_path]
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in watched}
        listing_before = sorted(p.name for p in self.root.iterdir())

        model_report.build_report(self.center_path, self.cindy_path)

        for path, (payload, mtime) in before.items():
            self.assertEqual(path.read_bytes(), payload, f"{path.name} 被写过")
            self.assertEqual(path.stat().st_mtime_ns, mtime, f"{path.name} 的 mtime 变了")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), listing_before)

    def test_center_db_without_required_tables_names_the_missing_ones(self):
        broken = self.root / "空库.sqlite3"
        sqlite3.connect(broken).close()

        with self.assertRaises(model_report.ReportError) as caught:
            model_report.build_report(broken, self.cindy_path)

        self.assertIn("tickets", str(caught.exception))

    def test_default_center_db_walks_up_from_a_worktree(self):
        """零参数能用的前提：中心库按脚本位置往上找，不钉在脚本旁边、也不看当前目录。"""
        main_checkout = self.root / "仓库"
        (main_checkout / ".data").mkdir(parents=True)
        expected = main_checkout / ".data" / "task-center.sqlite3"
        expected.write_bytes(b"")
        worktree = main_checkout / ".worktrees" / "T-1"
        worktree.mkdir(parents=True)

        self.assertEqual(model_report.default_center_db(worktree), expected.resolve())

    def test_default_center_db_takes_the_nearest_data_dir(self):
        """多层 .data 时取最近的一份，不是最上面那份。"""
        upper = self.root / ".data"
        upper.mkdir()
        (upper / "task-center.sqlite3").write_bytes(b"")
        lower = self.root / "子仓库" / ".data"
        lower.mkdir(parents=True)
        expected = lower / "task-center.sqlite3"
        expected.write_bytes(b"")
        start = self.root / "子仓库" / "a" / "b"
        start.mkdir(parents=True)

        self.assertEqual(model_report.default_center_db(start), expected.resolve())


if __name__ == "__main__":
    unittest.main()
