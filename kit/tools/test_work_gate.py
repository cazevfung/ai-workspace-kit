"""工单闸门：没有已绑定的执行尝试，改动不该落盘。

这一组用例锁住的是「触发」而不是「记得」：闸门必须能拒绝没开工单、领了没绑、
绑在别的项目上的情况，而不只是打印一句提醒。
"""
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import center_config
import project_doctor
import work_gate


import contextlib
import io
import os
import subprocess
from datetime import datetime, timedelta, timezone


def resolve(ids, payload):
    """把 payload 里的短名换成库发的工单号（create_ticket 不再收自报号）。"""
    alias = payload.get("ticket_id")
    real = ids.get(alias) if isinstance(alias, str) else None
    return {**payload, "ticket_id": real} if real else payload


def create_numbered(ok, ids, alias, payload):
    """建一张单并把库发的号记进 ids[alias]；payload 里不带 ticket_id。"""
    created = ok("create_ticket", payload)
    ids[alias] = created["id"]
    return created


class WorkGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "仓库"
        self.project.mkdir()
        (self.project / "PRD").mkdir()
        (self.project / "PRD" / "PRD-001-示例.md").write_text("需求", encoding="utf-8")
        self.other = self.root / "另一个项目"
        self.other.mkdir()
        (self.other / "PRD.md").write_text("需求", encoding="utf-8")
        self.db = self.root / "center.sqlite3"
        self.counter = 0
        # 发号之后，测试里仍用 repo-1 / other-1 这类短名指代工单。
        self.ids = {}
        self.ok("init", {})
        self.ok("register_project", {"project_id": "repo", "name": "仓库", "root": str(self.project)})
        self.ok("register_project", {"project_id": "other", "name": "另一个", "root": str(self.other)})
        for pid, path in (("repo", self.project), ("other", self.other)):
            create_numbered(self.ok, self.ids, f"{pid}-1", {
                "project_id": pid, "title": "改动工具",
                "deliverable": "一个会拒绝无工单提交的闸门",
                "prd_path": "PRD/PRD-001-示例.md" if pid == "repo" else "PRD.md",
                "criteria": [{"id": "C1", "text": "含 `python -m task_center --help` 且退出码 0"}],
            })

    def tid(self, alias):
        """短名 → 库发的号。"""
        return self.ids[alias]

    def ok(self, operation, payload):
        self.counter += 1
        payload = resolve(self.ids, payload)
        result = work_gate.load_center()(self.db).execute(operation, payload, request_id=f"req-{self.counter}")
        self.assertTrue(result["ok"], result)
        return result["result"]

    def check(self, ticket, project=None, repo=None):
        return work_gate.verify(self.db, ticket, project=project,
                                repo=repo or self.project)

    def bind(self, pid="repo"):
        # claim 现在必须写明派发者是谁，且绑定会话不能是同一个（SELF_DISPATCH）。
        claim = self.ok("claim_ticket", {"project_id": pid, "ticket_id": f"{pid}-1", "expected_revision": 1,
                                        "coordinator_session_id": f"coordinator-{pid}"})
        self.ok("bind_session", {"project_id": pid, "ticket_id": f"{pid}-1", "attempt_id": claim["attempt_id"],
                                 "session_id": f"session-{pid}", "project_root": str(self.project if pid == "repo" else self.other)})
        return claim["attempt_id"]

    def test_no_hidden_kill_switch(self):
        """`.task-center-enforce` 这种隐身开关已被删：缺文件就静默放行，比没钩子更危险。"""
        self.assertFalse((Path(work_gate.REPO) / ".task-center-enforce").exists())

    def test_hook_blocks_a_commit_without_a_ticket(self):
        """端到端：真的跑一次 git commit，让钩子自己说话。"""
        self.git("init", "-q")
        self.git("config", "core.hooksPath", str(Path(work_gate.REPO) / ".githooks"))
        result = self.git("-c", "user.name=t", "-c", "user.email=t@t",
                          "commit", "--allow-empty", "-m", "没有工单", env={"TASK_CENTER_DB": str(self.db)})
        self.assertNotEqual(0, result.returncode, result)
        self.assertIn("提交被拦", result.stderr)

    def test_hook_allows_a_commit_with_a_bound_ticket(self):
        self.bind("repo")
        self.git("init", "-q")
        self.git("config", "core.hooksPath", str(Path(work_gate.REPO) / ".githooks"))
        result = self.git("-c", "user.name=t", "-c", "user.email=t@t",
                          "commit", "--allow-empty", "-m", "有工单",
                          env={"TASK_CENTER_DB": str(self.db), "TASK_CENTER_TICKET": self.tid("repo-1")})
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("放行", result.stdout + result.stderr)

    def test_missing_ticket_id_is_rejected_before_touching_the_database(self):
        # 这条要走完收尾提交判定才轮到「没有工单号」，而收尾判定是拿
        # `git diff --cached` 读暂存区的。源码包（Release 的 source.zip）解压出来
        # 的目录不是 git 工作树，git 在那里自己就报错，闸门会在更早一步拒掉——
        # 那是环境不成立，不是行为变了。所以这里 skip 而不是把断言改成迁就环境：
        # 断言本身留着，在真正的仓库里照旧生效（AWK-115）。
        if not project_doctor.is_repository(work_gate.REPO):
            self.skipTest(
                f"不在 git 工作树里（{work_gate.REPO}）：收尾判定读不到暂存区，"
                "这条用例的前提不成立。想看它真跑，先在这个目录 `git init` 并提交一次。"
            )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = work_gate.main(["work_gate.py", "--db", str(self.db)])
        self.assertEqual(1, code)
        self.assertIn("没有工单号", stderr.getvalue())

    def test_forced_other_project_cannot_lend_its_ticket(self):
        """残留的 TASK_CENTER_PROJECT 不能把 B 项目工单借给 A 项目目录。"""
        self.bind("other")
        ok, payload = self.check(self.tid("other-1"), project="other")
        self.assertFalse(ok, payload)
        self.assertEqual("PROJECT_MISMATCH", payload["error"])

    def test_submitted_ticket_cannot_authorise_more_edits(self):
        attempt = self.bind("repo")
        (self.project / "out.md").write_text("成果", encoding="utf-8")
        self.ok("submit_result", {"project_id": "repo", "ticket_id": "repo-1", "attempt_id": attempt,
                                  "session_id": "session-repo", "summary": "成果", "artifacts": ["out.md"],
                                  "evidence": ["核对"], "risks": [],
                                  "head_sha": "0123456789abcdef0123456789abcdef01234567",
                                  "base_sha": "89abcdef0123456789abcdef0123456789abcdef"})
        ok, payload = self.check(self.tid("repo-1"))
        self.assertFalse(ok, payload)
        self.assertIn("recover_ticket", payload["fix"])

    def git(self, *args, env=None):
        clean = {k: v for k, v in os.environ.items() if not k.startswith("TASK_CENTER")}
        clean.update(env or {})
        return subprocess.run(["git", *args], cwd=self.project, encoding="utf-8", errors="replace",
                              capture_output=True, env=clean, timeout=120)

    def test_bound_running_ticket_passes_and_names_what_it_authorises(self):
        attempt = self.bind()
        ok, payload = self.check(self.tid("repo-1"))
        self.assertTrue(ok, payload)
        self.assertEqual(self.tid("repo-1"), payload["ticket_id"])
        self.assertEqual(attempt, payload["attempt_id"])
        self.assertEqual("session-repo", payload["session_id"])
        self.assertEqual("PRD/PRD-001-示例.md", payload["prd_path"])

    def test_ready_ticket_without_any_attempt_is_rejected(self):
        ok, payload = self.check(self.tid("repo-1"))
        self.assertFalse(ok, payload)
        self.assertIn("claim", payload["fix"])

    def test_claimed_but_unbound_ticket_is_rejected(self):
        self.ok("claim_ticket", {"project_id": "repo", "ticket_id": "repo-1", "expected_revision": 1,
                              "coordinator_session_id": "session-coordinator"})
        ok, payload = self.check(self.tid("repo-1"))
        self.assertFalse(ok, payload)
        self.assertIn("bind_session", payload["fix"])

    def test_unknown_ticket_is_rejected(self):
        ok, payload = self.check("ghost")
        self.assertFalse(ok, payload)
        self.assertIn("创建工单", payload["fix"])

    def test_ticket_belonging_to_another_project_is_rejected(self):
        self.bind("other")
        ok, payload = self.check(self.tid("other-1"))
        self.assertFalse(ok, payload)
        self.assertIn("PROJECT_MISMATCH", json.dumps(payload, ensure_ascii=False))

    def test_missing_database_names_the_problem(self):
        ok, payload = work_gate.verify(self.root / "nope.sqlite3", self.tid("repo-1"), repo=self.project)
        self.assertFalse(ok, payload)
        self.assertIn("center.json", payload["fix"])

    def test_list_shows_active_attempts_only(self):
        self.bind("repo")
        items = work_gate.active(self.db)
        self.assertEqual([self.tid("repo-1")], [item["ticket_id"] for item in items])
        self.assertEqual("running", items[0]["state"])


class ListCommandTest(unittest.TestCase):
    """`--list` 回答的是「现在有没有人在做」，读不了库时必须当场失败。

    以前 `active()` 读库失败就 `return []`，`--list` 于是打印零行、退出 0：库坏了、
    版本对不上、升到一半，它一律报「没有活跃尝试」。这条命令正是「开工前先查有没有
    人在做」的判据，它撒谎的直接后果是两个人同时改同一批文件。和 AW-020（测试目录
    没了 run_tests 还说过）是同一类病：静默失败比报错贵。

    两份库都是副本，真实库一步不碰。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "仓库"
        self.project.mkdir()
        (self.project / "PRD").mkdir()
        (self.project / "PRD" / "PRD-001-示例.md").write_text("需求", encoding="utf-8")
        self.db = self.root / "center.sqlite3"
        self.counter = 0
        # 发号之后，测试里仍用 repo-1 / other-1 这类短名指代工单。
        self.ids = {}
        self.ok("init", {})
        self.ok("register_project", {"project_id": "repo", "name": "仓库", "root": str(self.project)})
        create_numbered(self.ok, self.ids, "repo-1", {
            "project_id": "repo", "title": "改动工具",
            "deliverable": "一个不撒谎的活跃尝试清单",
            "prd_path": "PRD/PRD-001-示例.md",
            "criteria": [{"id": "C1", "text": "含 `python -m task_center --help` 且退出码 0"}],
        })
        claim = self.ok("claim_ticket", {"project_id": "repo", "ticket_id": "repo-1", "expected_revision": 1,
                                       "coordinator_session_id": "coordinator-repo"})
        self.ok("bind_session", {"project_id": "repo", "ticket_id": "repo-1",
                                 "attempt_id": claim["attempt_id"], "session_id": "session-repo",
                                 "project_root": str(self.project)})

    def tid(self, alias):
        """短名 → 库发的号。"""
        return self.ids[alias]

    def ok(self, operation, payload):
        self.counter += 1
        payload = resolve(self.ids, payload)
        result = work_gate.load_center()(self.db).execute(operation, payload, request_id=f"req-{self.counter}")
        self.assertTrue(result["ok"], result)
        return result["result"]

    def copy_database(self, name):
        """用 sqlite 自带的 backup 复制一份：库开着 WAL，直接拷文件会丢掉还没落盘的记录。"""
        target = self.root / name
        source = sqlite3.connect(str(self.db))
        self.addCleanup(source.close)
        copy = sqlite3.connect(str(target))
        with copy:
            source.backup(copy)
        copy.close()
        return target

    def list_command(self, db, *extra):
        # `--repo` 必须显式给：不给时 work_gate 退回它自己所在的那个检出，于是这个
        # 用例会去读**真实仓库**的 `tasks/round.md`。那份文件天然写在合并之前、可能
        # 正写了一半（AW-123），而它决定不了这里的任何一条断言。给成本用例自己的
        # 临时项目，读的就是这个项目自己的（不存在的）轮次文件。
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = work_gate.main(
                ["work_gate.py", "--list", "--db", str(db), "--repo", str(self.project), *extra]
            )
        return code, out.getvalue(), err.getvalue()

    def backdate_last_event(self, db, minutes):
        """把 repo-1 的事件时间推到 N 分钟前：只改副本上的那几行，真实库不碰。"""
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        with contextlib.closing(sqlite3.connect(str(db))) as conn:
            with conn:
                conn.execute("UPDATE events SET created_at=? WHERE ticket_id=?", (stamp, self.tid("repo-1")))

    def test_copy_with_a_wrong_schema_fails_loudly(self):
        """v6 副本给 v7 代码读：必须非零退出，stderr 里看得见错误码。"""
        broken = self.copy_database("center-v6.sqlite3")
        with contextlib.closing(sqlite3.connect(str(broken))) as conn:
            conn.execute("PRAGMA user_version=6")
            conn.commit()
        code, out, err = self.list_command(broken)
        self.assertEqual(1, code, out + err)
        self.assertIn("SCHEMA_MISMATCH", err)
        self.assertEqual("", out, "读不了库时不能一边报错一边照旧打印空清单")
        self.assertIn("不是「没有活跃尝试」", err)

    def test_corrupt_copy_fails_loudly(self):
        """文件损坏是另一个错误码，同样不许被当成「没人干活」。"""
        broken = self.root / "center-corrupt.sqlite3"
        broken.write_bytes(b"not a database at all")
        code, out, err = self.list_command(broken)
        self.assertEqual(1, code, out + err)
        self.assertIn("STORAGE_ERROR", err)
        self.assertEqual("", out)

    def test_active_raises_instead_of_returning_an_empty_list(self):
        """根因在 `active()` 自己：空列表与「读不出来」必须是两回事。"""
        broken = self.copy_database("center-v6-active.sqlite3")
        with contextlib.closing(sqlite3.connect(str(broken))) as conn:
            conn.execute("PRAGMA user_version=6")
            conn.commit()
        with self.assertRaises(work_gate.CenterUnreadable) as caught:
            work_gate.active(broken)
        self.assertEqual("SCHEMA_MISMATCH", caught.exception.code)

    def test_readable_copy_still_lists_and_exits_zero(self):
        """正常可读时行为一字不变：照旧列出，退出 0。"""
        good = self.copy_database("center-copy.sqlite3")
        code, out, err = self.list_command(good)
        self.assertEqual(0, code, out + err)
        self.assertIn(self.tid("repo-1"), out)
        self.assertIn("running", out)
        self.assertIn("session-repo", out)

    # --- AW-087 / PRD-013 R4：等了多少、谁该动手，看得见 ------------------

    def test_list_table_prints_six_columns_with_parseable_timestamps(self):
        """第四列之后补「尝试开始时间」「最后事件时间」；表格行必须是 6 列时间戳。"""
        good = self.copy_database("center-cols.sqlite3")
        code, out, err = self.list_command(good)
        self.assertEqual(0, code, out + err)
        rows = [line for line in out.splitlines() if line.strip()]
        self.assertTrue(rows, out)
        for line in rows:
            fields = line.split("\t")
            self.assertEqual(6, len(fields), line)
            for value in fields[4:]:
                # 占位文字（例如「-」）过不了这一关：要的是能算间隔的时间。
                self.assertIsNotNone(datetime.fromisoformat(value), line)

    def test_list_json_is_really_json_and_carries_both_timestamps(self):
        """`--json` 在 `--list` 分支里以前被忽略：打的是表格，json.load 直接抛。"""
        good = self.copy_database("center-json.sqlite3")
        code, out, err = self.list_command(good, "--json")
        self.assertEqual(0, code, out + err)
        payload = json.loads(out)
        self.assertTrue(payload["ok"], payload)
        items = payload["result"]["items"]
        self.assertGreaterEqual(len(items), 1)
        for item in items:
            self.assertIn("started_at", item)
            self.assertIn("last_event_at", item)
            self.assertIsNotNone(datetime.fromisoformat(item["started_at"]))
            self.assertIsNotNone(datetime.fromisoformat(item["last_event_at"]))

    def test_stalled_minutes_turns_a_stale_ticket_into_a_failure(self):
        """判据是退出码，不是输出措辞：最后事件 46 分钟前 + 阈值 45 必须退出 1。"""
        stale = self.copy_database("center-stale.sqlite3")
        self.backdate_last_event(stale, 46)
        code, out, err = self.list_command(stale, "--stalled-minutes", "45")
        self.assertEqual(1, code, out + err)
        self.assertIn("疑似停住", out + err)
        # 不传阈值时不据它判红：这一条挡的是「退出码恒为 1」
        code, out, err = self.list_command(stale)
        self.assertEqual(0, code, out + err)

    def test_stalled_minutes_stays_green_below_the_threshold(self):
        fresh = self.copy_database("center-fresh.sqlite3")
        code, out, err = self.list_command(fresh, "--stalled-minutes", "45")
        self.assertEqual(0, code, out + err)

    def test_list_summary_counts_how_many_tickets_are_stalled(self):
        """R4 要求末尾报「共 N 张在途，其中 M 张最后落库超过 45 分钟」。"""
        stale = self.copy_database("center-summary.sqlite3")
        self.backdate_last_event(stale, 46)
        code, out, err = self.list_command(stale)
        self.assertEqual(0, code, out + err)
        self.assertIn("共 1 张在途", err)
        self.assertIn("其中 1 张最后落库超过 45 分钟", err)
        # 汇总行走 stderr：stdout 只有 6 列表格，机器解析不会被最后一行绊倒。
        self.assertNotIn("共 1 张在途", out)

    # --- AW-109：池子里逐张点名，年龄线过了就判红 -------------------------

    def planned(self, alias="repo-plan-1", criteria=None, days_old=0):
        """造一张排期单：`criteria=None` 用一条可判定条件，`[]` 就是「念头」。"""
        ticket = create_numbered(self.ok, self.ids, alias, {
            "project_id": "repo", "title": "排在池子里的单", "state": "planned",
            "deliverable": "一张只有轮到它才开工的单",
            "prd_path": "PRD/PRD-001-示例.md",
            "criteria": ([{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}]
                         if criteria is None else criteria),
        })
        if days_old:
            self.backdate_created(ticket["id"], days_old)
        return ticket

    def backdate_created(self, ticket_id, days):
        """把池子里那张单的 created_at 推到 N 天前：只改副本/临时库上的那一行。"""
        stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with contextlib.closing(sqlite3.connect(str(self.db))) as conn:
            with conn:
                conn.execute("UPDATE tickets SET created_at=? WHERE id=?", (stamp, ticket_id))

    def test_list_names_every_planned_ticket_in_the_pool(self):
        """C2：池子里有单时 `--list` 要逐张点名（不只是数一个数字），且退出 0。"""
        self.planned()
        code, out, err = self.list_command(self.db, "--repo", str(self.project))
        self.assertEqual(0, code, out + err)
        self.assertIn(self.tid("repo-plan-1"), err)
        self.assertIn("1 条条件", err)
        # 明细同样走 stderr：stdout 仍然只有那张六列表格。
        self.assertNotIn(self.tid("repo-plan-1"), out)

    def test_list_marks_a_pool_ticket_without_criteria(self):
        """刚落下的空条件单要认得出来：它是念头，只能补条件或取消，领不走。"""
        self.planned("repo-plan-empty", criteria=[])
        code, out, err = self.list_command(self.db, "--repo", str(self.project))
        self.assertEqual(0, code, out + err)
        self.assertIn(self.tid("repo-plan-empty"), err)
        self.assertIn("条件空着", err)
        self.assertIn("revise_ticket", err)

    def test_list_turns_red_when_the_oldest_pool_ticket_is_too_old(self):
        """池子年龄线：排够 7 天的单必须报出来并判红，否则「加入口」只是把蒸发换成长草。"""
        self.planned("repo-plan-old", days_old=work_gate.PLANNED_MAX_DAYS + 1)
        code, out, err = self.list_command(self.db, "--repo", str(self.project))
        self.assertEqual(1, code, out + err)
        self.assertIn(self.tid("repo-plan-old"), err)
        self.assertIn("pool-stale", err)

    def test_list_stays_green_just_under_the_pool_line(self):
        """年龄线是「超过」，不是「等于」：刚好在线上的一天不判红。"""
        self.planned("repo-plan-young")
        code, out, err = self.list_command(self.db, "--repo", str(self.project))
        self.assertEqual(0, code, out + err)
        self.assertIn(self.tid("repo-plan-young"), err)
        self.assertIn(f"年龄线 {work_gate.PLANNED_MAX_DAYS} 天", err)


class MergeCommitGateTest(unittest.TestCase):
    """合并提交要能过钩子，但只限「真的在一次合并里、工单已验收、还没合并记录」。

    合并发生在验收之后，所以那一刻工单已经是 done；只看 running 会把每一次合并都
    拦下来。但也不能把 done 直接放开：`done` 之后任何普通改动都不该落盘。区别就在于
    仓库里到底有没有没结束的合并（MERGE_HEAD），所以这一组用**真实的合并状态**来测。
    """

    GIT_ENV_BASE = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.example",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.example"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "仓库"
        (self.project / "PRD").mkdir(parents=True)
        (self.project / "PRD" / "PRD-001-示例.md").write_text("需求", encoding="utf-8")
        self.db = self.root / "center.sqlite3"
        self.counter = 0
        # 发号之后，测试里仍用 repo-1 / other-1 这类短名指代工单。
        self.ids = {}
        self.ok("init", {})
        self.ok("register_project", {"project_id": "repo", "name": "仓库", "root": str(self.project)})
        create_numbered(self.ok, self.ids, "repo-1", {
            "project_id": "repo", "title": "改动工具",
            "deliverable": "一个会拒绝无工单提交的闸门",
            "prd_path": "PRD/PRD-001-示例.md",
            "criteria": [{"id": "C1", "text": "含 `python -m task_center --help` 且退出码 0"}],
        })

    # --- 小工具 -----------------------------------------------------------

    def tid(self, alias):
        """短名 → 库发的号。"""
        return self.ids[alias]

    def ok(self, operation, payload):
        self.counter += 1
        payload = resolve(self.ids, payload)
        result = work_gate.load_center()(self.db).execute(operation, payload, request_id=f"req-{self.counter}")
        self.assertTrue(result["ok"], result)
        return result["result"]

    def git(self, *args, env=None):
        clean = {k: v for k, v in os.environ.items() if not k.startswith("TASK_CENTER")}
        clean.update(self.GIT_ENV_BASE)
        clean.update(env or {})
        return subprocess.run(["git", *args], cwd=str(self.project), encoding="utf-8", errors="replace",
                              capture_output=True, env=clean, timeout=120)

    def init_repo(self):
        self.git("init", "-q", "-b", "master")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")

    def bind(self):
        claim = self.ok("claim_ticket", {"project_id": "repo", "ticket_id": "repo-1", "expected_revision": 1,
                                       "coordinator_session_id": "coordinator-repo"})
        self.ok("bind_session", {"project_id": "repo", "ticket_id": "repo-1",
                                 "attempt_id": claim["attempt_id"], "session_id": "session-repo",
                                 "project_root": str(self.project)})
        return claim["attempt_id"]

    def done(self):
        """推到 done：提交成果 + 另一个会话验收通过。"""
        attempt = self.bind()
        (self.project / "out.md").write_text("成果", encoding="utf-8")
        submission = self.ok("submit_result", {
            "project_id": "repo", "ticket_id": "repo-1", "attempt_id": attempt,
            "session_id": "session-repo", "summary": "成果", "artifacts": ["out.md"],
            "evidence": ["核对"], "risks": [],
            "head_sha": "0123456789abcdef0123456789abcdef01234567",
            "base_sha": "0123456789abcdef0123456789abcdef01234567",
        })
        self.ok("review_result", {
            "project_id": "repo", "ticket_id": "repo-1", "attempt_id": attempt,
            "submission_id": submission["submission_id"], "reviewer_session_id": "reviewer-repo",
            "verdict": "accept", "reason": "通过",
            "checks": [{"id": "C1", "passed": True, "evidence": "核对了"}],
        })
        detail = work_gate.load_center()(self.db).execute(
            "get_ticket", {"project_id": "repo", "ticket_id": self.tid("repo-1")})
        self.assertEqual("done", detail["result"]["state"], detail)
        # schema 8 起「没过回执的单」写不进合并记录；这些用例判的是合并闸门，先把回执补上。
        self.ok("record_notice", {
            "project_id": "repo", "ticket_id": "repo-1", "attempt_id": attempt,
            "session_id": "coordinator-repo", "origin_session_id": "coordinator-repo",
            "status": "sent", "detail": "已把回执发回原会话",
            "target_session_id": "coordinator-repo", "wake_kind": "queued",
        })

    def start_conflicted_merge(self):
        """造一个真实的「合并进行中」：两边改同一个文件，merge --no-commit 留下 MERGE_HEAD。"""
        self.init_repo()
        self.git("checkout", "-q", "-b", "side")
        (self.project / "PRD" / "PRD-001-示例.md").write_text("side", encoding="utf-8")
        self.git("commit", "-q", "-am", "side")
        self.git("checkout", "-q", "master")
        (self.project / "PRD" / "PRD-001-示例.md").write_text("master", encoding="utf-8")
        self.git("commit", "-q", "-am", "master")
        result = self.git("merge", "--no-commit", "side")
        self.assertNotEqual(0, result.returncode, "这次合并应该冲突，冲突才会留下 MERGE_HEAD")
        self.assertTrue((self.project / ".git" / "MERGE_HEAD").is_file())

    def gate(self):
        """像钩子那样跑一次闸门，返回 (退出码, stdout, stderr)。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = work_gate.main([
                "work_gate.py", "--ticket", self.tid("repo-1"), "--db", str(self.db),
                "--repo", str(self.project),
            ])
        return code, out.getvalue(), err.getvalue()

    # --- 普通提交：规则不变 -----------------------------------------------

    def test_done_ticket_cannot_authorise_a_normal_commit(self):
        """没有 MERGE_HEAD 时，done 工单依旧拦下普通提交。"""
        # 闸门现在要先看暂存区（判这是不是一次收尾提交），所以这个检出得先是个仓库。
        self.init_repo()
        self.done()
        code, out, err = self.gate()
        self.assertEqual(1, code, out + err)
        self.assertIn("TICKET_DONE", err)

    def test_merge_head_does_not_authorise_an_unclaimed_ticket(self):
        """半合状态不是万用通行证：工单还没领取，MERGE_HEAD 存在也照拒。"""
        self.start_conflicted_merge()
        code, out, err = self.gate()
        self.assertEqual(1, code, out + err)
        self.assertIn("claim", err)

    def test_merge_head_does_not_authorise_claimed_but_unbound(self):
        self.ok("claim_ticket", {"project_id": "repo", "ticket_id": "repo-1", "expected_revision": 1,
                              "coordinator_session_id": "coordinator-repo"})
        self.start_conflicted_merge()
        code, out, err = self.gate()
        self.assertEqual(1, code, out + err)
        self.assertIn("bind_session", err)

    def test_merge_head_does_not_authorise_a_recorded_merge_twice(self):
        """已经记过合并的工单不该出现第二次合并提交。"""
        self.done()
        self.ok("merge_ticket", {"project_id": "repo", "ticket_id": "repo-1",
                                 "commit": "a" * 40, "target_branch": "master", "tests": "OK",
                                 "reviewed_sha": "0123456789abcdef0123456789abcdef01234567"})
        self.start_conflicted_merge()
        code, out, err = self.gate()
        self.assertEqual(1, code, out + err)
        self.assertIn("ALREADY_MERGED", err)

    # --- 合并提交：放行 ---------------------------------------------------

    def test_merge_in_progress_is_detected_from_the_git_directory(self):
        self.start_conflicted_merge()
        self.assertTrue(work_gate.merge_in_progress(self.project))
        self.assertFalse(work_gate.merge_in_progress(self.root))

    def test_merge_commit_for_a_done_ticket_is_allowed_and_says_why(self):
        """C3：MERGE_HEAD + done + 无合并记录 → 放行，并说清这是合并提交。"""
        self.done()
        self.start_conflicted_merge()
        code, out, err = self.gate()
        self.assertEqual(0, code, out + err)
        self.assertIn(f"合并提交：{self.tid('repo-1')}", out)

    def test_running_ticket_is_still_allowed_while_a_merge_is_open(self):
        """MERGE_HEAD 在不在都不影响普通规则：running + 已绑定本会话照样放行。"""
        self.bind()
        self.start_conflicted_merge()
        ok, payload = work_gate.verify(self.db, self.tid("repo-1"), repo=self.project)
        self.assertTrue(ok, payload)
        self.assertEqual("running", payload["mode"])

    def test_hook_lets_a_real_merge_commit_through(self):
        """端到端：一次真实的 merge --no-ff 提交，钩子自己说话。"""
        self.done()
        self.init_repo()
        self.git("checkout", "-q", "-b", "side")
        (self.project / "side.txt").write_text("side", encoding="utf-8")
        self.git("add", "side.txt")
        self.git("commit", "-q", "-m", "side")
        self.git("checkout", "-q", "master")
        (self.project / "master.txt").write_text("master", encoding="utf-8")
        self.git("add", "master.txt")
        self.git("commit", "-q", "-m", "master")
        merged = self.git("merge", "--no-commit", "--no-ff", "side")
        self.assertEqual(0, merged.returncode, merged.stderr)
        self.assertTrue((self.project / ".git" / "MERGE_HEAD").is_file())
        self.git("config", "core.hooksPath", str(Path(work_gate.REPO) / ".githooks"))
        result = self.git("commit", "-m", "合并 side",
                          env={"TASK_CENTER_DB": str(self.db), "TASK_CENTER_TICKET": self.tid("repo-1")})
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("合并提交：", result.stdout + result.stderr)
        parents = self.git("rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
        self.assertEqual(3, len(parents), "应该真的产生了一个两父提交的合并提交")

    def test_decide_is_a_pure_choice(self):
        """判定本身抽成了纯函数：四种输入组合各自说清结果。"""
        decide = work_gate.decide
        self.assertEqual("merging", decide("done", bound=False, in_merge=True, has_merge=False))
        self.assertEqual("running", decide("running", bound=True, in_merge=False, has_merge=False))
        self.assertEqual("running", decide("running", bound=True, in_merge=True, has_merge=False))
        self.assertEqual("reject", decide("done", bound=False, in_merge=False, has_merge=False))
        self.assertEqual("reject", decide("done", bound=True, in_merge=True, has_merge=True))
        self.assertEqual("reject", decide("ready", bound=False, in_merge=True, has_merge=False))


class CenterCodeOrderTest(unittest.TestCase):
    """闸门用的是哪份 task_center 代码，必须能当场看出来。

    旧顺序（环境变量 → .worktrees/ticket-core → 仓库根）在代码已合回 master 之后
    仍然优先工作树：工作树落后 master 时会**静默用旧代码判放行**，闸门看起来在拦，
    实际跑的是另一个版本。这里锁住「根目录优先，工作树只作兜底」。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.worktree = self.repo / ".worktrees" / "ticket-core"
        self.explicit = self.repo.parent / (self.repo.name + "-explicit")
        self.addCleanup(lambda: None)

    def plant(self, base, marker):
        """造一份能被识别为 task_center 代码的目录，里面带一个可辨认的标记。"""
        code = base / "task_center"
        code.mkdir(parents=True, exist_ok=True)
        (code / "__init__.py").write_text(f"MARKER = {marker!r}\n", encoding="utf-8")
        return base

    def marker_of(self, base):
        return (Path(base) / "task_center" / "__init__.py").read_text(encoding="utf-8")

    def test_repo_root_wins_over_a_stale_worktree(self):
        """根目录有代码时就用根目录那份，不管工作树在不在。"""
        self.plant(self.repo, "root")
        self.plant(self.worktree, "worktree")
        chosen = work_gate.find_center_root(repo=self.repo, env={})
        self.assertEqual(self.repo.resolve(), Path(chosen).resolve())
        self.assertIn("root", self.marker_of(chosen))

    def test_worktree_is_only_a_fallback(self):
        """根目录没有 task_center 时，才回退到工作树。"""
        self.plant(self.worktree, "worktree")
        chosen = work_gate.find_center_root(repo=self.repo, env={})
        self.assertEqual(self.worktree.resolve(), Path(chosen).resolve())

    def test_explicit_code_directory_has_top_priority(self):
        """显式给了 TASK_CENTER_CODE，就听它的，哪怕根目录也有代码。"""
        self.plant(self.repo, "root")
        self.plant(self.worktree, "worktree")
        self.plant(self.explicit, "explicit")
        chosen = work_gate.find_center_root(repo=self.repo, env={"TASK_CENTER_CODE": str(self.explicit)})
        self.assertEqual(self.explicit.resolve(), Path(chosen).resolve())
        self.assertIn("explicit", self.marker_of(chosen))

    def test_no_code_anywhere_returns_none(self):
        self.assertIsNone(work_gate.find_center_root(repo=self.repo, env={}))

    def test_candidate_order_is_root_before_worktree(self):
        """顺序本身也要锁住，避免以后又被改回去。"""
        order = [Path(item).resolve() for item in work_gate.center_candidates(repo=self.repo, env={})]
        self.assertEqual([self.repo.resolve(), self.worktree.resolve()], order)


class ClosingCommitGateTest(unittest.TestCase):
    """收尾提交：第三条放行路径，窄到能逐条核对。

    收尾包只能在「本项目没有任何工单在飞」的时候提交（`close_gate` 要求无在途单），
    而普通改动必须有绑定在 `running` 尝试上的工单。两者不可能同时成立，所以收尾
    提交要像合并提交那样有一条被承认的例外。例外必须窄：只认那三个路径、`tasks/
    round.md` 只认状态行、`tasks/closing-*.md` 至少一份、本项目没有任何在途单。

    拿不到暂存区、拿不到库、读不到轮次行时一律拒绝并写明原因——一处读不出来就放行，
    等于给所有提交开一个「没报错就是通过」的旁路。
    """

    GIT_ENV_BASE = MergeCommitGateTest.GIT_ENV_BASE

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "仓库"
        (self.project / "tasks").mkdir(parents=True)
        (self.project / "PRD").mkdir()
        (self.project / "PRD" / "PRD-001-示例.md").write_text("需求", encoding="utf-8")
        self.round = self.project / "tasks" / "round.md"
        self.write_round()
        self.db = self.root / "center.sqlite3"
        self.counter = 0
        # 发号之后，测试里仍用 repo-1 / other-1 这类短名指代工单。
        self.ids = {}
        self.ok("init", {})
        self.ok("register_project", {"project_id": "repo", "name": "仓库", "root": str(self.project)})
        self.git("init", "-q", "-b", "master")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")

    # --- 小工具 -----------------------------------------------------------

    def tid(self, alias):
        """短名 → 库发的号。"""
        return self.ids[alias]

    def ok(self, operation, payload):
        self.counter += 1
        payload = resolve(self.ids, payload)
        result = work_gate.load_center()(self.db).execute(operation, payload, request_id=f"req-{self.counter}")
        self.assertTrue(result["ok"], result)
        return result["result"]

    def git(self, *args, env=None):
        clean = {k: v for k, v in os.environ.items() if not k.startswith("TASK_CENTER")}
        clean.update(self.GIT_ENV_BASE)
        clean.update(env or {})
        return subprocess.run(["git", *args], cwd=str(self.project), encoding="utf-8", errors="replace",
                              capture_output=True, env=clean, timeout=120)

    def write_round(self, status="进行中", goals="1. 让收尾真的会发生"):
        self.round.write_text(
            "# 本轮：收尾\n\n轮次：R-900\n状态：" + status + "\n\n"
            "## 本轮目标\n\n" + goals + "\n\n"
            "## 结束条件\n\n- python tools/close_gate.py 退出 0\n\n"
            "## 不做什么\n\n- 不改 PRD\n",
            encoding="utf-8",
        )

    def packet(self, name="closing-R-900.md"):
        path = self.project / "tasks" / name
        path.write_text("# 收尾包\n\n## 7. 结论\n\n已收尾\n", encoding="utf-8")
        return path

    def closing(self):
        return work_gate.closing_commit(self.project, self.db)

    def planned(self, alias="repo-plan-1", state="planned"):
        """造一张没开工的排期单：收尾不该被它挡住（PRD-014 R1 / AC-R1-1）。"""
        return create_numbered(self.ok, self.ids, alias, {
            "project_id": "repo", "title": "排在池子里的单", "state": state,
            "deliverable": "一张只有到了轮到它的时候才开工的单",
            "prd_path": "PRD/PRD-001-示例.md",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        })

    def bind(self):
        """造一张在途工单：收尾提交要求本项目一张在飞的单都没有。"""
        create_numbered(self.ok, self.ids, "repo-1", {
            "project_id": "repo", "title": "改动工具",
            "deliverable": "一条能被核对的收尾提交路径",
            "prd_path": "PRD/PRD-001-示例.md",
            "criteria": [{"id": "C1", "text": "含 `python -m task_center --help` 且退出码 0"}],
        })
        claim = self.ok("claim_ticket", {"project_id": "repo", "ticket_id": "repo-1", "expected_revision": 1,
                                        "coordinator_session_id": "coordinator-repo"})
        self.ok("bind_session", {"project_id": "repo", "ticket_id": "repo-1",
                                 "attempt_id": claim["attempt_id"], "session_id": "session-repo",
                                 "project_root": str(self.project)})
        return claim["attempt_id"]

    # --- 放行 -------------------------------------------------------------

    def test_closing_commit_allows_only_closing_files(self):
        """只带收尾包 + 状态行：放行，并说清这是哪一轮。"""
        self.packet()
        self.write_round(status="已收尾")
        self.git("add", "-A")
        verdict = self.closing()
        self.assertEqual("allow", verdict["verdict"], verdict)
        self.assertEqual("R-900", verdict["round"])
        self.assertIn("收尾提交：R-900", verdict["detail"])

    def test_closing_commit_allows_the_ledger_line(self):
        """账本 tasks/rounds.md 也在放开清单里。"""
        self.packet()
        self.write_round(status="已收尾")
        (self.project / "tasks" / "rounds.md").write_text("| R-900 | 2026-11-18 | 收尾 | 已收尾 |\n", encoding="utf-8")
        self.git("add", "-A")
        self.assertEqual("allow", self.closing()["verdict"])

    def test_closing_packet_change_is_allowed_when_a_running_ticket_backs_it(self):
        """AW-124 现场：收尾包是成果本体的工单在 running 期间提交被 CLOSING_REJECTED。

        只改 `tasks/closing-R-017.md`（补记追加目标）、`tasks/round.md` 的状态位不动，
        而本项目正有一张绑定在 `running` 尝试上的工单在干这件事。这不是收尾提交，
        是一条普通改动：必须按那张工单放行，不该被收尾提交的前提拦住。
        """
        self.bind()
        self.packet()
        self.git("add", "-A")
        verdict = self.closing()
        self.assertEqual("allow", verdict["verdict"], verdict)
        self.assertEqual("packet", verdict.get("mode"), verdict)
        self.assertIn(self.tid("repo-1"), verdict["detail"])
        self.assertIn("不是收尾", verdict["detail"])

    def close_round_in_base(self):
        """把「已收尾」写进基线：之后暂存 `进行中` 才是「从三档改回进行中」。"""
        self.write_round(status="已收尾")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "收尾 R-900")

    def test_closing_packet_change_detail_says_status_untouched(self):
        """round.md 没进暂存区时，提示可以说「状态位没变」——那就是真实情况。"""
        self.bind()
        self.packet()
        self.git("add", "-A")
        verdict = self.closing()
        self.assertEqual("allow", verdict["verdict"], verdict)
        self.assertIn("没有动", verdict["detail"])
        self.assertIn("状态位", verdict["detail"])

    def test_closing_packet_change_may_reopen_the_round_status(self):
        """把状态位从三档改回 `进行中`（重开一轮）走的是工单，不是收尾例外。

        这一支都带 `tasks/round.md`，但判据差一件事：状态位有没有改成三档之一。
        改成 `进行中` 时状态位真的动了，所以提示不能再写「状态位没动」；
        放行的依据依旧只有一张绑定在 `running` 尝试上的工单。
        """
        self.close_round_in_base()
        self.bind()
        self.packet()
        self.write_round(status="进行中")
        self.git("add", "-A")
        verdict = self.closing()
        self.assertEqual("allow", verdict["verdict"], verdict)
        self.assertEqual("packet", verdict.get("mode"), verdict)
        self.assertIn(self.tid("repo-1"), verdict["detail"])
        self.assertIn("进行中", verdict["detail"])
        self.assertNotIn("状态位没动", verdict["detail"])

    def test_reopening_the_round_without_a_ticket_is_rejected(self):
        """同一暂存区、没有工单：重开一轮也是普通改动，不能自拿走尾例外。"""
        self.close_round_in_base()
        self.packet()
        self.write_round(status="进行中")
        self.git("add", "-A")
        verdict = self.closing()
        self.assertEqual("reject", verdict["verdict"], verdict)
        self.assertIn("running", verdict["detail"])
        self.assertIn("工单", verdict["detail"])

    def test_closing_packet_change_still_rejects_round_body_edit(self):
        """带上收尾包之后，round.md 仍只许改状态行；本轮目标一起改就拒。"""
        self.bind()
        self.packet()
        self.write_round(status="进行中", goals="1. 换成更好达成的那个目标")
        self.git("add", "-A")
        verdict = self.closing()
        self.assertEqual("reject", verdict["verdict"], verdict)
        self.assertIn("状态", verdict["detail"])

    # --- 拒收 -------------------------------------------------------------

    def test_closing_commit_rejects_extra_path(self):
        """带上别的文件就不是收尾提交，要点名那个路径。"""
        self.packet()
        self.write_round(status="已收尾")
        (self.project / "tools").mkdir()
        (self.project / "tools" / "work_gate.py").write_text("# 夹带的改动\n", encoding="utf-8")
        self.git("add", "-A")
        verdict = self.closing()
        self.assertEqual("reject", verdict["verdict"], verdict)
        self.assertIn("tools/work_gate.py", verdict["detail"])

    def test_closing_commit_rejects_in_flight(self):
        """本项目还有在途工单时不能收尾：close_gate 的 --check 也不会过。"""
        self.bind()
        self.packet()
        self.write_round(status="已收尾")
        self.git("add", "-A")
        verdict = self.closing()
        self.assertEqual("reject", verdict["verdict"], verdict)
        self.assertIn("在途", verdict["detail"])
        self.assertIn(self.tid("repo-1"), verdict["detail"])

    def test_closing_commit_allows_a_ticket_that_is_only_planned(self):
        """池子里排着的单不挡收尾：这就是 AW-094 那类现场不再逼人取消单子。"""
        self.planned()
        self.packet()
        self.write_round(status="已收尾")
        self.git("add", "-A")
        verdict = self.closing()
        self.assertEqual("allow", verdict["verdict"], verdict)
        self.assertEqual("R-900", verdict["round"])

    def test_closing_commit_still_rejects_a_ready_ticket_nobody_claimed(self):
        """放行的只是 `planned`：`ready` 是「等人领走」，还是没干完的活。"""
        self.planned("repo-ready-1", state="ready")
        self.packet()
        self.write_round(status="已收尾")
        self.git("add", "-A")
        verdict = self.closing()
        self.assertEqual("reject", verdict["verdict"], verdict)
        self.assertIn(self.tid("repo-ready-1"), verdict["detail"])
        self.assertIn("ready", verdict["detail"])

    def test_closing_packet_change_without_any_ticket_is_rejected(self):
        """同一暂存区（只含收尾包）、一张工单都没有：没有东西能背书，仍然拒。

        状态位没动，所以它不是收尾提交；而要让收尾包改动落地，只能靠一张绑定在
        `running` 尝试上的工单。两样都没有时放行就等于给收尾包开一条无依据的旁路。
        """
        self.packet()
        self.git("add", "-A")
        verdict = self.closing()
        self.assertEqual("reject", verdict["verdict"], verdict)
        self.assertIn("收尾包", verdict["detail"])
        self.assertIn("工单", verdict["detail"])
        self.assertIn("不是收尾提交", verdict["detail"])

    def test_a_ready_ticket_does_not_back_a_closing_packet_change(self):
        """`ready` 是「等人领走」，不是「有人在做」，不能给收尾包改动背书。"""
        self.planned("repo-ready-1", state="ready")
        self.packet()
        self.git("add", "-A")
        verdict = self.closing()
        self.assertEqual("reject", verdict["verdict"], verdict)
        self.assertIn("running", verdict["detail"])

    def test_closing_commit_rejects_round_body_edit(self):
        """状态行之外的改动不许借这条路走：本轮目标改了也要拒。"""
        self.packet()
        self.write_round(status="已收尾", goals="1. 换成更好达成的那个目标")
        self.git("add", "-A")
        verdict = self.closing()
        self.assertEqual("reject", verdict["verdict"], verdict)
        self.assertIn("状态", verdict["detail"])

    def test_closing_commit_requires_a_packet(self):
        """只改状态位不算收尾：没有 tasks/closing-*.md 就拒。"""
        self.write_round(status="已收尾")
        self.git("add", "-A")
        verdict = self.closing()
        self.assertEqual("reject", verdict["verdict"], verdict)
        self.assertIn("closing-", verdict["detail"])

    def test_closing_commit_rejects_when_the_round_number_is_missing(self):
        """读不到 `轮次：R-00N` 就说不出这是哪一轮，拒绝并点名。"""
        self.packet()
        self.round.write_text("# 本轮\n\n状态：已收尾\n\n## 本轮目标\n\n1. x\n", encoding="utf-8")
        self.git("add", "-A")
        verdict = self.closing()
        self.assertEqual("reject", verdict["verdict"], verdict)
        self.assertIn("轮次", verdict["detail"])

    def test_closing_commit_rejects_an_unreadable_center(self):
        """库读不了时不知道有没有在途单：拒绝，不是放行。"""
        self.packet()
        self.write_round(status="已收尾")
        self.git("add", "-A")
        verdict = work_gate.closing_commit(self.project, self.root / "nope.sqlite3")
        self.assertEqual("reject", verdict["verdict"], verdict)
        self.assertIn("库", verdict["detail"])

    # --- 钩子端到端 -------------------------------------------------------

    def hooks(self):
        self.git("config", "core.hooksPath", str(Path(work_gate.REPO) / ".githooks"))

    def test_hook_lets_a_closing_commit_through_without_a_ticket(self):
        """真正的目的：没有 TASK_CENTER_TICKET 也能提交收尾包，钩子自己说这是收尾提交。"""
        self.packet()
        self.write_round(status="已收尾")
        self.git("add", "-A")
        self.hooks()
        result = self.git("commit", "-m", "收尾 R-900", env={"TASK_CENTER_DB": str(self.db)})
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("收尾提交：R-900", result.stdout + result.stderr)

    def test_hook_allows_a_closing_commit_with_a_planned_ticket_in_the_pool(self):
        """钩子不因为库里留着一张 planned 单而拒（C6）：2026-11-21 收尾 R-015 就是那样。

        那次 AW-094 留在 `ready`，收尾提交被连拒三次，最后只能把它 `cancel_ticket`
        才收得了尾。`planned` 落库之后，同样现场必须能提交。
        """
        self.planned()
        self.packet()
        self.write_round(status="已收尾")
        self.git("add", "-A")
        self.hooks()
        result = self.git("commit", "-m", "收尾 R-900", env={"TASK_CENTER_DB": str(self.db)})
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("收尾提交：R-900", result.stdout + result.stderr)

    def test_hook_lets_a_ticket_backed_closing_packet_through(self):
        """AW-124 的真实现场：带 TASK_CENTER_TICKET 的会话改收尾包，钩子必须放行。

        那次改动只能靠 `git commit --no-verify` 落盘，钩子从头到尾没有给过一条
        能走的路——这条用例钉住的就是那条路真的存在。
        """
        self.bind()
        self.packet()
        self.git("add", "-A")
        self.hooks()
        result = self.git("commit", "-m", "补记收尾包",
                          env={"TASK_CENTER_DB": str(self.db),
                               "TASK_CENTER_TICKET": self.tid("repo-1")})
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("收尾包", result.stdout + result.stderr)

    def test_hook_still_blocks_a_ticketless_commit_with_an_extra_path(self):
        """例外没变成后门：夹带别的文件、又没有工单，照旧拦。"""
        self.packet()
        self.write_round(status="已收尾")
        (self.project / "tools").mkdir()
        (self.project / "tools" / "work_gate.py").write_text("# 夹带的改动\n", encoding="utf-8")
        self.git("add", "-A")
        self.hooks()
        result = self.git("commit", "-m", "收尾 + 夹带", env={"TASK_CENTER_DB": str(self.db)})
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("提交被拦", result.stderr)


class UnclosedRoundListTest(unittest.TestCase):
    """`--list` 要回答「本轮收了没有」：这是每轮开工的第一条命令。

    收尾包闸门存在之后，仍然没有任何东西会去调它——`状态：进行中` 可以合法地永远
    留着。这条判定挂在 `--list` 上，因为它是每轮开工真正会跑的第一条命令。
    """

    GIT_ENV_BASE = MergeCommitGateTest.GIT_ENV_BASE

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "仓库"
        (self.project / "tasks").mkdir(parents=True)
        (self.project / "PRD").mkdir()
        (self.project / "PRD" / "PRD-001-示例.md").write_text("需求", encoding="utf-8")
        self.round = self.project / "tasks" / "round.md"
        self.set_status("进行中")
        self.db = self.root / "center.sqlite3"
        self.counter = 0
        # 发号之后，测试里仍用 repo-1 / other-1 这类短名指代工单。
        self.ids = {}
        self.ok("init", {})
        self.ok("register_project", {"project_id": "repo", "name": "仓库", "root": str(self.project)})
        create_numbered(self.ok, self.ids, "repo-1", {
            "project_id": "repo", "title": "改动工具", "state": "planned",
            "deliverable": "一个会非零的收尾判定",
            "prd_path": "PRD/PRD-001-示例.md",
            "criteria": [{"id": "C1", "text": "含 `python -m task_center --help` 且退出码 0"}],
        })

    def tid(self, alias):
        """短名 → 库发的号。"""
        return self.ids[alias]

    def ok(self, operation, payload):
        self.counter += 1
        payload = resolve(self.ids, payload)
        result = work_gate.load_center()(self.db).execute(operation, payload, request_id=f"req-{self.counter}")
        self.assertTrue(result["ok"], result)
        return result["result"]

    def set_status(self, status):
        self.round.write_text(
            "# 本轮：收尾\n\n轮次：R-900\n状态：" + status + "\n\n"
            "## 本轮目标\n\n1. 让收尾真的会发生\n\n"
            "## 结束条件\n\n- python tools/close_gate.py 退出 0\n\n"
            "## 不做什么\n\n- 不改 PRD\n",
            encoding="utf-8",
        )

    def land_and_merge(self):
        """造一张 done 且已有合并记录的工单：这才叫「这一轮真的做出过东西」。"""
        claim = self.ok("claim_ticket", {"project_id": "repo", "ticket_id": "repo-1", "expected_revision": 1,
                                       "coordinator_session_id": "coordinator-repo"})
        attempt = claim["attempt_id"]
        self.ok("bind_session", {"project_id": "repo", "ticket_id": "repo-1", "attempt_id": attempt,
                                 "session_id": "session-repo", "project_root": str(self.project)})
        (self.project / "out.md").write_text("成果", encoding="utf-8")
        submission = self.ok("submit_result", {
            "project_id": "repo", "ticket_id": "repo-1", "attempt_id": attempt,
            "session_id": "session-repo", "summary": "成果", "artifacts": ["out.md"],
            "evidence": ["核对"], "risks": [],
            "head_sha": "0123456789abcdef0123456789abcdef01234567",
            "base_sha": "0123456789abcdef0123456789abcdef01234567",
        })
        self.ok("review_result", {
            "project_id": "repo", "ticket_id": "repo-1", "attempt_id": attempt,
            "submission_id": submission["submission_id"], "reviewer_session_id": "reviewer-repo",
            "verdict": "accept", "reason": "通过",
            "checks": [{"id": "C1", "passed": True, "evidence": "核对了"}],
        })
        self.ok("record_notice", {
            "project_id": "repo", "ticket_id": "repo-1", "attempt_id": attempt,
            "session_id": "coordinator-repo", "origin_session_id": "coordinator-repo",
            "status": "sent", "detail": "已把回执发回原会话",
            "target_session_id": "coordinator-repo", "wake_kind": "queued",
        })
        self.ok("merge_ticket", {"project_id": "repo", "ticket_id": "repo-1",
                                 "commit": "a" * 40, "target_branch": "master", "tests": "OK",
                                 "reviewed_sha": "0123456789abcdef0123456789abcdef01234567"})

    def list_command(self):
        return self.run_main(["--list", "--db", str(self.db), "--repo", str(self.project)])

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = work_gate.main(["work_gate.py", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_list_flags_unclosed_round(self):
        """没有在途单、有 done 且已合并的工单、状态还是进行中：非零并给出三步。"""
        self.land_and_merge()
        code, out, err = self.list_command()
        self.assertEqual(1, code, out + err)
        self.assertIn("本轮 R-900 未收尾", out + err)
        self.assertIn("closing-R-900.md", out + err)
        self.assertIn("收尾再开新单", out + err)

    # -- 在途集合：planned 不算、ready 算（PRD-014 AC-R1-2）-------------------

    def test_list_reports_zero_in_flight_when_only_planned_is_queued(self):
        """池子里只排着一张 planned：退出 0，并打印「在途 0 张」。"""
        code, out, err = self.list_command()
        self.assertEqual(0, code, out + err)
        self.assertIn("在途 0 张", err)
        self.assertIn("1 张 planned 排着，不算在途", err)
        # 它没有尝试，所以不在那张表里：池子里的单只在那一句里计数，明细看
        # `list_tickets --planned`（PRD-014 R6）。
        self.assertNotIn(self.tid("repo-1"), out)

    def test_list_fails_and_names_a_ready_ticket(self):
        """同一张单改成 ready：同一条命令非零，并点名这张单（钩子也是这么判的）。"""
        create_numbered(self.ok, self.ids, "repo-2", {
            "project_id": "repo", "title": "等人领走的单",
            "deliverable": "一张已经可以开工但还没人接的单",
            "prd_path": "PRD/PRD-001-示例.md",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        })
        code, out, err = self.list_command()
        self.assertEqual(1, code, out + err)
        self.assertIn(self.tid("repo-2"), err)
        self.assertIn("在等人领走", err)
        self.assertIn("在途 1 张", err)
        # planned 那一张不出现在点名的名单里：放行的只是它。
        self.assertNotIn(f"{self.tid('repo-1')}（planned）", err)

    def test_list_json_carries_the_in_flight_set(self):
        """--json 也要能看出这六个格各几张，不然机器没法核对。"""
        _code, payload, err = self.list_json()
        in_flight = payload["result"]["in_flight"]
        self.assertEqual(0, in_flight["total"])
        self.assertEqual(1, in_flight["planned"])
        self.assertEqual([], in_flight["waiting"])
        self.assertEqual(
            ["dispatching", "running", "awaiting_review", "blocked", "ready", "rework"],
            list(in_flight["states"]),
        )

    def test_list_passes_when_round_closed(self):
        """状态位已经是三档之一：退出 0，不再提这件事。"""
        self.land_and_merge()
        self.set_status("已收尾")
        code, out, err = self.list_command()
        self.assertEqual(0, code, out + err)
        self.assertNotIn("未收尾", out + err)

    def test_list_still_passes_while_a_ticket_is_in_flight(self):
        """还有在途单时这一轮本来就没法收，不报「未收尾」。"""
        claim = self.ok("claim_ticket", {"project_id": "repo", "ticket_id": "repo-1", "expected_revision": 1,
                                       "coordinator_session_id": "coordinator-repo"})
        self.ok("bind_session", {"project_id": "repo", "ticket_id": "repo-1",
                                 "attempt_id": claim["attempt_id"], "session_id": "session-repo",
                                 "project_root": str(self.project)})
        code, out, err = self.list_command()
        self.assertEqual(0, code, out + err)
        self.assertNotIn("未收尾", out + err)

    # --json 里的 ok 必须与退出码一致 -------------------------------------------

    def list_json(self):
        code, out, err = self.run_main(
            ["--list", "--json", "--db", str(self.db), "--repo", str(self.project)]
        )
        return code, json.loads(out), err

    def test_list_json_unclosed_round_is_not_ok(self):
        """「本轮未收尾」现场：退出 1 时 JSON 不能说 ok=true，并要写清为什么。

        以前 JSON 先打印、收尾判定在后面单走 return 1，于是同一份回执一边写
        ok=true 一边退 1：调用方看哪个都能得出相反的结论。
        """
        self.land_and_merge()
        code, payload, err = self.list_json()
        self.assertEqual(1, code, err)
        self.assertFalse(payload["ok"], f"退出 1 时 ok 必须为 false：{payload}")
        reason = payload["result"].get("reason", "")
        self.assertIn("unclosed-round", reason, f"失败要写清原因：{payload}")
        self.assertIn("R-900", reason, f"原因里要点名轮次：{payload}")

    def test_list_json_ok_and_exit_code_agree_when_clear(self):
        """现场没问题时两者也要一致：ok=true 且退出 0，不写 reason。"""
        self.land_and_merge()
        self.set_status("已收尾")
        code, payload, err = self.list_json()
        self.assertEqual(0, code, err)
        self.assertTrue(payload["ok"], f"退出 0 时 ok 必须为 true：{payload}")
        self.assertNotIn("reason", payload["result"], f"通过时不该写失败原因：{payload}")

    def test_list_json_stalled_threshold_and_exit_code_agree(self):
        """另一种失败源（停住阈值）同样要 ok=false + reason，不能只挂一个退出码。"""
        self.land_and_merge()
        self.set_status("已收尾")
        # 另一张在途单：造「有人在做」的现场，才能用阈值判它停住。
        create_numbered(self.ok, self.ids, "repo-2", {
            "project_id": "repo", "title": "另一张",
            "deliverable": "造一个在途单",
            "prd_path": "PRD/PRD-001-示例.md",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        })
        claim = self.ok("claim_ticket", {"project_id": "repo", "ticket_id": "repo-2",
                                       "expected_revision": 1,
                                       "coordinator_session_id": "coordinator-repo"})
        self.ok("bind_session", {"project_id": "repo", "ticket_id": "repo-2",
                                 "attempt_id": claim["attempt_id"], "session_id": "session-repo",
                                 "project_root": str(self.project)})
        # 把这张单的最后事件时间拉到很久以前：刚建的单不算「停住」。
        # 连接要显式关：`with sqlite3.connect(...)` 只提交事务，不关文件；
        # Windows 上没关的句柄会让临时目录删不掉（PermissionError WinError 32）。
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute("UPDATE events SET created_at='2020-01-01T00:00:00+00:00' WHERE ticket_id=?",
                         (self.tid("repo-2"),))
            conn.execute("UPDATE attempts SET created_at='2020-01-01T00:00:00+00:00' WHERE ticket_id=?",
                         (self.tid("repo-2"),))
            conn.commit()
        finally:
            conn.close()
        code, out, err = self.run_main(
            ["--list", "--json", "--stalled-minutes", "1",
             "--db", str(self.db), "--repo", str(self.project)]
        )
        payload = json.loads(out)
        self.assertEqual(1, code, err)
        self.assertFalse(payload["ok"], f"退出 1 时 ok 必须为 false：{payload}")
        self.assertIn("stalled", payload["result"].get("reason", ""), f"要点名停住：{payload}")


class CenterDiscoveryTest(unittest.TestCase):
    """中心库怎么发现：只有一处，且要认安装时的 --agents-home（AWK-105 C1/C2/C4）。

    这一组的现场按真实布局摆：`<工作区>/.agents/center.json` 是指针（install.py 写的），
    `<agents-home>/center.json` 说的是库在哪，`~/.agents/center.json` 是默认档。
    所有用例都把 `home` / `env` / `roots` 显式传进去，一个真实用户目录都不读。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.ws = base / "ws"
        (self.ws / ".agents").mkdir(parents=True)
        self.home = base / "home"
        self.agents_home = base / "agents"
        self.agents_home.mkdir()
        self.pointer = self.ws / ".agents" / "center.json"
        self.commanded_db = base / "custom" / "center.sqlite3"
        self.default_db = base / "default" / "center.sqlite3"
        self.own_db = self.ws / ".data" / "task-center.sqlite3"
        self.write_center(self.agents_home, self.commanded_db)
        self.write_center(self.home / ".agents", self.default_db)

    def write_center(self, agents_home, db):
        agents_home.mkdir(parents=True, exist_ok=True)
        (agents_home / "center.json").write_text(
            json.dumps({"db": str(db)}, ensure_ascii=False), encoding="utf-8")

    def write_pointer(self, agents_home):
        self.pointer.write_text(json.dumps({"agents_home": str(agents_home)}, ensure_ascii=False),
                                encoding="utf-8")

    def resolve(self, **kwargs):
        payload = {"repo": self.ws, "env": {}, "home": self.home}
        payload.update(kwargs)
        return work_gate.discover_db(**payload)

    def test_custom_agents_home_wins_over_default(self):
        """C1：--agents-home 装出来的工作区，读到的是 <agents-home>/center.json 的库。"""
        self.write_pointer(self.agents_home)
        found = self.resolve()
        self.assertEqual(self.commanded_db, found.db)
        self.assertIn("工作区指针", found.source)
        self.assertEqual(self.agents_home, found.agents_home)

    def test_missing_agents_home_config_is_fatal_and_does_not_fall_back(self):
        """C2：指针指到的 center.json 不在，就报 DB_NOT_FOUND，不回落 ~/.agents。"""
        self.write_pointer(self.agents_home)
        (self.agents_home / "center.json").unlink()
        with self.assertRaises(center_config.DiscoveryError) as caught:
            self.resolve()
        error = caught.exception
        self.assertEqual("DB_NOT_FOUND", error.code)
        # 说得出「缺的是哪一个」：不接受一句「没找到库」。
        self.assertIn(str(self.agents_home / "center.json"), error.message)
        self.assertIn("不回落到 ~/.agents", error.fix)
        # 而且确实没有偷偷返回默认档那份库。
        with self.assertRaises(center_config.DiscoveryError):
            self.resolve()

    def test_default_install_still_reads_user_agents(self):
        """C4：默认安装（不传 --agents-home）仍读 ~/.agents，行为不变。"""
        self.write_pointer(self.home / ".agents")
        found = self.resolve()
        self.assertEqual(self.default_db, found.db)

    def test_install_without_pointer_falls_back_to_user_agents(self):
        """老安装没有工作区指针：照旧读 ~/.agents。"""
        found = self.resolve()
        self.assertEqual(self.default_db, found.db)

    def test_workspace_own_database_wins_over_user_agents(self):
        """工作区自带库（install 初始化的那一份）优先于 ~/.agents。"""
        self.own_db.parent.mkdir(parents=True)
        self.own_db.write_bytes(b"")
        found = self.resolve()
        # Windows 的临时目录会回 8.3 短路径（202108~1），比较前先归一。
        self.assertEqual(self.own_db.resolve(), found.db.resolve())

    def test_explicit_paths_beat_the_pointer(self):
        """--db 与 TASK_CENTER_DB 是人当场说的，优先级最高。"""
        self.write_pointer(self.agents_home)
        explicit = self.ws.parent / "explicit.sqlite3"
        self.assertEqual(explicit, self.resolve(explicit=str(explicit)).db)
        env_db = self.ws.parent / "env.sqlite3"
        self.assertEqual(env_db, self.resolve(env={"TASK_CENTER_DB": str(env_db)}).db)

    def test_explicit_agents_home_env(self):
        """TASK_CENTER_AGENTS_HOME：多份安装共用一台机器时显式指定。"""
        other = self.ws.parent / "other-agents"
        self.write_center(other, self.ws.parent / "other.sqlite3")
        found = self.resolve(env={"TASK_CENTER_AGENTS_HOME": str(other)})
        self.assertEqual(self.ws.parent / "other.sqlite3", found.db)

    def test_worktree_finds_pointer_in_main_checkout(self):
        """工作树里没有指针（`.agents/center.json` 不进版本库）就回主检出找那一份。"""
        if shutil.which("git") is None:
            self.skipTest("没有 git")
        base = self.ws.parent / "gitrepo"
        base.mkdir()
        (base / ".data").mkdir()
        self.assertTrue(self.git(base, "init", "-b", "master")[0] == 0)
        (base / "README.md").write_text("x", encoding="utf-8")
        self.git(base, "add", "-A")
        self.git(base, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-m", "init")
        worktree = self.ws.parent / "wt"
        code, _out, err = self.git(base, "worktree", "add", "-q", str(worktree), "-b", "t-wt")
        self.assertEqual(0, code, err)
        pointer = base / ".agents" / "center.json"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(json.dumps({"agents_home": str(self.agents_home)}, ensure_ascii=False),
                           encoding="utf-8")
        found = work_gate.discover_db(repo=worktree, env={}, home=self.home)
        self.assertEqual(self.commanded_db, found.db)
        self.assertFalse((worktree / ".agents" / "center.json").exists())

    def git(self, repo, *args):
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=120)
        return result.returncode, result.stdout, result.stderr

    def test_list_command_exits_non_zero_with_db_not_found(self):
        """C2 的命令形态：库里找不到就非零退出，并打出 DB_NOT_FOUND 与缺的那个文件。"""
        self.write_pointer(self.agents_home)
        (self.agents_home / "center.json").unlink()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = work_gate.main(["work_gate.py", "--list", "--repo", str(self.ws)])
        self.assertEqual(1, code, out.getvalue() + err.getvalue())
        self.assertIn("DB_NOT_FOUND", err.getvalue())
        self.assertIn(str(self.agents_home / "center.json"), err.getvalue())
        # 不许「新建一个空库」：现场一个文件都没多出来。
        self.assertFalse(self.own_db.exists())


if __name__ == "__main__":
    unittest.main()
