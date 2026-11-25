"""工单闸门：没有已绑定的执行尝试，改动不该落盘。

这一组用例锁住的是「触发」而不是「记得」：闸门必须能拒绝没开工单、领了没绑、
绑在别的项目上的情况，而不只是打印一句提醒。
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import work_gate
import project_doctor


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


class PagesTest(unittest.TestCase):
    """`pages()` 必须翻页翻完：只取第一页会让最新建的单永远不在清单里（AW-163）。"""

    def test_pages_walks_past_the_first_hundred(self):
        tickets = [{"id": f"T-{index:03d}"} for index in range(1, 126)]

        class Fake:
            def __init__(self):
                self.offsets = []

            def execute(self, operation, payload=None, request_id=None):
                self.offsets.append(payload["offset"])
                page = tickets[payload["offset"]:payload["offset"] + payload["limit"]]
                return {"ok": True, "result": {"items": page}}

        center = Fake()
        got = work_gate.pages(center, "p1")
        self.assertEqual([item["id"] for item in tickets], [item["id"] for item in got])
        self.assertEqual([0, 100], center.offsets, "第一页满 100 张就必须接着翻")


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

    # --- AW-165：整库读不出时按现场分诊，不再一律「先修库」 -------------------

    def test_corrupt_center_advice_is_restore_not_a_blanket_fix(self):
        """库文件坏了：劝人恢复快照或换 --db，不再句句「先修库（migrate 或恢复快照）」。

        起因现场是坏行，不是库坏；AW-163 把那一类分开之后，能走到这一处的只剩 schema
        不符、库文件坏、锁/权限/未知几类，而它们的出路并不相同——一句「先修库」在其中
        几类是反方向。这条同时钉住 AW-163 已给的部分不许回退：错误码、出错的那一步与
        存储层异常类型都要照旧打得出来，否则「按现场分诊」就成了让人自己去猜。
        """
        broken = self.root / "center-corrupt-triage.sqlite3"
        broken.write_bytes(b"not a database at all")
        code, out, err = self.list_command(broken)
        self.assertEqual(1, code, out + err)
        self.assertIn("读库失败：STORAGE_ERROR", err, "错误码照旧打印")
        self.assertIn("出错的一步：list_projects", err, "哪一步出错要看得出")
        self.assertIn("file is not a database", err, "报文原文不许丢")
        self.assertIn("DatabaseError", err, "异常类型也要说得出来")
        self.assertNotIn("先修库（migrate 或恢复快照）", err, "不再是无条件的一句建议")
        self.assertIn("快照", err, "文件坏了的第一条出路是恢复快照")
        self.assertIn("--db", err, "第二条是换一份库")
        self.assertEqual("", out, "读不了库时不能一边报错一边照旧打印空清单")

    def test_schema_mismatch_advice_names_migrate(self):
        """schema 对不上：出路是 migrate，而不是让去恢复快照。"""
        broken = self.copy_database("center-v6-triage.sqlite3")
        with contextlib.closing(sqlite3.connect(str(broken))) as conn:
            conn.execute("PRAGMA user_version=6")
            conn.commit()
        code, out, err = self.list_command(broken)
        self.assertEqual(1, code, out + err)
        self.assertIn("SCHEMA_MISMATCH", err)
        self.assertIn("migrate", err)
        self.assertNotIn("先修库（migrate 或恢复快照）", err, "不再是无条件的一句建议")
        self.assertNotIn("文件层快照", err, "schema 不符不是文件坏，别劝人去恢复快照")

    def test_locked_center_advice_points_at_the_scene_not_at_a_guess(self):
        """锁 / 权限 / 未知这一档：照操作名与异常类型定位，不给一个方向。

        这条同时走的是**轮次**那个分支（`--list` 里第二处「整库读不出」）：本轮写在
        另一个项目名下，那个项目的工单列表读不出来。两处现场是同一个、出路也是同一条，
        所以两边必须给同一套分诊——各写一份就必然一边改了、一边没改（AW-165）。
        """
        other = self.root / "另一个仓库"
        other.mkdir()
        (other / "tasks").mkdir()
        (other / "tasks" / "round.md").write_text(
            "轮次：R-901\n状态：进行中\n项目：other\n", encoding="utf-8")
        tasks = self.project / "tasks"
        tasks.mkdir()
        (tasks / "round.md").write_text(
            "轮次：R-901\n状态：进行中\n项目：other\n", encoding="utf-8")
        self.ok("register_project", {"project_id": "other", "name": "另一个仓库", "root": str(other)})

        center_class = work_gate.load_center()
        original_loader = work_gate.load_center

        class Locked(center_class):
            def execute(self, operation, payload=None, request_id=None):
                if operation == "list_tickets" and (payload or {}).get("project_id") == "other":
                    return {"ok": False,
                            "error": {"code": "STORAGE_ERROR", "message": "database is locked"}}
                return super().execute(operation, payload, request_id)

        work_gate.load_center = lambda: Locked
        self.addCleanup(setattr, work_gate, "load_center", original_loader)

        code, out, err = self.list_command(self.copy_database("center-locked.sqlite3"))
        self.assertEqual(1, code, out + err)
        self.assertIn("读库失败：STORAGE_ERROR", err)
        self.assertIn("出错的一步：list_tickets", err, "锁住的是哪一步要看得见")
        self.assertIn("database is locked", err, "报文原文不许丢")
        self.assertIn("判不出这一轮收没收", err, "这一处判的是轮次，不是在途表")
        self.assertIn("锁 / 权限", err, "这一档本来就该让人去定位，而不是给一个方向")
        self.assertNotIn("先修库（migrate 或恢复快照）", err, "不再是无条件的一句建议")
        self.assertNotIn("文件层快照", err, "锁不是文件坏，别劝人去恢复快照")

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

    # --- AW-163：一张单读不出来，不该让整份在途清单读不出来 -------------------

    def flaky_center(self, broken_ticket, message):
        """让某个工单号的 `get_ticket` 失败，其余照常：现场就是这么坏的（HSE-007 的旧提交）。"""
        center_class = work_gate.load_center()
        original_loader = work_gate.load_center

        class Flaky(center_class):
            def execute(self, operation, payload=None, request_id=None):
                if operation == "get_ticket" and (payload or {}).get("ticket_id") == broken_ticket:
                    return {"ok": False, "error": {"code": "INVALID_INPUT", "message": message}}
                return super().execute(operation, payload, request_id)

        work_gate.load_center = lambda: Flaky
        self.addCleanup(setattr, work_gate, "load_center", original_loader)

    def test_one_unreadable_ticket_still_lists_the_rest(self):
        """一张单读不出来：其余照常打印、汇总行照旧，坏的那张逐条点名（AW-163）。

        反例是修之前的行为——整张在途清单被一行 `INVALID_INPUT` 顶掉，所有人都
        看不见别人在做什么。这里要三件事同时成立：其余在途单还在表里、汇总行仍在、
        坏的那条带错误码与报文被点名，而「读库失败」四个字不出现（那是库本身读不了）。
        """
        create_numbered(self.ok, self.ids, "repo-2", {
            "project_id": "repo", "title": "池子里的另一张",
            "deliverable": "一张没开工的单", "prd_path": "PRD/PRD-001-示例.md",
            "criteria": [{"id": "C1", "text": "含 `python -m task_center --help` 且退出码 0"}],
            "state": "planned",
        })
        broken = self.tid("repo-2")
        self.flaky_center(broken, "Invalid input or inaccessible local path — JSONDecodeError: Expecting value")
        good = self.copy_database("center-one-broken.sqlite3")
        code, out, err = self.list_command(good)
        self.assertIn(self.tid("repo-1"), out, "其余在途单照旧打印")
        self.assertIn("共 1 张在途", err, "汇总行照旧给出张数")
        self.assertIn("另有 1 张读不出来", err)
        self.assertIn(f"读不出来：单 {broken}", err)
        self.assertIn("INVALID_INPUT", err)
        self.assertIn("JSONDecodeError", err, "报文里的底层异常类型不丢")
        self.assertNotIn("读库失败", err, "库是读得通的，只是这一张单坏")
        self.assertEqual(1, code, "读不出来的单必须能被判成失败，不能悄悄当成没有")

    def test_json_lists_the_unreadable_ticket(self):
        """`--json` 也要答得出「哪一张没读到」，否则机器调用方只能看到少了一条。"""
        create_numbered(self.ok, self.ids, "repo-2", {
            "project_id": "repo", "title": "池子里的另一张",
            "deliverable": "一张没开工的单", "prd_path": "PRD/PRD-001-示例.md",
            "criteria": [{"id": "C1", "text": "含 `python -m task_center --help` 且退出码 0"}],
            "state": "planned",
        })
        broken = self.tid("repo-2")
        self.flaky_center(broken, "Invalid input or inaccessible local path — JSONDecodeError: Expecting value")
        code, out, err = self.list_command(self.copy_database("center-json-broken.sqlite3"), "--json")
        receipt = json.loads(out)
        self.assertFalse(receipt["ok"], receipt)
        self.assertEqual([broken], [entry["ticket_id"] for entry in receipt["result"]["unreadable"]])
        self.assertIn("unreadable", receipt["result"]["reason"])

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

    # --- 删除收尾包：不是收尾，是一条普通改动（AW-153 现场）----------------

    def committed_packet(self, name="closing-R-900.md"):
        """基线里先有一份收尾包：之后暂存它的删除才是「删已入库的收尾包」。"""
        self.packet(name)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "先有一份收尾包")

    def gate_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = work_gate.main(["work_gate.py", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_deleting_a_closing_packet_is_judged_as_a_normal_change(self):
        """AW-153：迁移时删掉搬走的收尾包，顺手带上别的文件就报 CLOSING_REJECTED。

        删除收尾包不是「收尾这个动作」——它没有新增任何收尾包，状态位也可能一动不动。
        所以它必须交回普通规则，由工单背书，而不是被当成一次收尾提交去核那三条前提。
        """
        self.committed_packet()
        (self.project / "tasks" / "closing-R-900.md").unlink()
        (self.project / "tasks" / "plan-100-搬走.md").write_text("计划\n", encoding="utf-8")
        self.git("add", "-A")
        verdict = self.closing()
        self.assertEqual("skip", verdict["verdict"], verdict)

    def test_deleting_a_closing_packet_alone_is_judged_as_a_normal_change(self):
        """只删收尾包、不带别的文件：同样不是收尾提交。

        它没有收尾包可核（这一支要求至少一份 `tasks/closing-*.md`），也没有状态位
        改动可核；按普通规则判，才不会把「删文件」当成「收尾」。
        """
        self.committed_packet()
        (self.project / "tasks" / "closing-R-900.md").unlink()
        self.git("add", "-A")
        verdict = self.closing()
        self.assertEqual("skip", verdict["verdict"], verdict)

    def test_deleting_a_packet_still_needs_a_ticket(self):
        """「按普通规则判」不是「随便放行」：没有工单号时这条提交照旧被拦。"""
        self.committed_packet()
        (self.project / "tasks" / "closing-R-900.md").unlink()
        self.git("add", "-A")
        code, out, err = self.gate_main("--repo", str(self.project), "--db", str(self.db))
        self.assertEqual(1, code, out + err)
        self.assertIn("工单", err)
        self.assertNotIn("CLOSING_REJECTED", err)

    def test_deleting_a_packet_with_a_running_ticket_commits_end_to_end(self):
        """真正的目的：删收尾包 + 别的文件，有一张 running 的单背书就能一次提交。"""
        self.committed_packet()
        self.bind()
        (self.project / "tasks" / "closing-R-900.md").unlink()
        (self.project / "tasks" / "plan-100-搬走.md").write_text("计划\n", encoding="utf-8")
        self.git("add", "-A")
        self.hooks()
        result = self.git("commit", "-m", "搬走收尾包",
                          env={"TASK_CENTER_DB": str(self.db),
                               "TASK_CENTER_TICKET": self.tid("repo-1")})
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("放行", result.stdout + result.stderr)

    def test_deleting_a_packet_does_not_launder_a_closing_with_extra_paths(self):
        """删一份收尾包不能把「收尾 + 夹带」洗成普通改动。

        现场：`git rm tasks/closing-R-900.md` + `round.md` 状态改成 `已收尾` +
        `tools/` 里一个文件 + 一张 `running` 的单。状态位真被改过，这就是一次收尾
        尝试：夹带要么拒，要么就得走收尾提交那三条前提——不能因为多了一条删除，
        就落回普通规则让那张单给它背书。
        """
        self.committed_packet()
        self.bind()
        (self.project / "tasks" / "closing-R-900.md").unlink()
        self.write_round(status="已收尾")
        (self.project / "tools").mkdir(exist_ok=True)
        (self.project / "tools" / "work_gate.py").write_text("# 夹带的改动\n", encoding="utf-8")
        self.git("add", "-A")
        verdict = self.closing()
        self.assertEqual("reject", verdict["verdict"], verdict)
        self.assertIn("tools/work_gate.py", verdict["detail"])

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

    def set_status(self, status, project=None):
        header = "" if project is None else f"项目：{project}\n"
        self.round.write_text(
            "# 本轮：收尾\n\n轮次：R-900\n状态：" + status + "\n" + header + "\n"
            "## 本轮目标\n\n1. 让收尾真的会发生\n\n"
            "## 结束条件\n\n- python tools/close_gate.py 退出 0\n\n"
            "## 不做什么\n\n- 不改 PRD\n",
            encoding="utf-8",
        )

    def other_project(self):
        """再登记一个项目：用它验证「轮次属于谁就按谁判」。"""
        other = self.root / "别的项目"
        (other / "tasks").mkdir(parents=True)
        (other / "PRD").mkdir()
        (other / "PRD" / "PRD-001-示例.md").write_text("需求", encoding="utf-8")
        self.ok("register_project", {"project_id": "other", "name": "别的项目", "root": str(other)})
        return other

    def write_other_round(self, root, status, number="R-900"):
        """写那个项目自己的 round.md：状态与落点必须来自同一个文件。"""
        (root / "tasks" / "round.md").write_text(
            "# 本轮：那边的收尾\n\n轮次：" + number + "\n状态：" + status + "\n项目：other\n\n"
            "## 本轮目标\n\n1. 那边的目标\n\n"
            "## 结束条件\n\n- python tools/close_gate.py 退出 0\n\n"
            "## 不做什么\n\n- 不改 PRD\n",
            encoding="utf-8",
        )

    def land_and_merge(self, project_id="repo", alias="repo-1", root=None, prefix="repo"):
        """造一张 done 且已有合并记录的工单：这才叫「这一轮真的做出过东西」。"""
        root = self.project if root is None else root
        claim = self.ok("claim_ticket", {"project_id": project_id, "ticket_id": alias, "expected_revision": 1,
                                       "coordinator_session_id": f"coordinator-{prefix}"})
        attempt = claim["attempt_id"]
        self.ok("bind_session", {"project_id": project_id, "ticket_id": alias, "attempt_id": attempt,
                                 "session_id": f"session-{prefix}", "project_root": str(root)})
        (root / "out.md").write_text("成果", encoding="utf-8")
        submission = self.ok("submit_result", {
            "project_id": project_id, "ticket_id": alias, "attempt_id": attempt,
            "session_id": f"session-{prefix}", "summary": "成果", "artifacts": ["out.md"],
            "evidence": ["核对"], "risks": [],
            "head_sha": "0123456789abcdef0123456789abcdef01234567",
            "base_sha": "0123456789abcdef0123456789abcdef01234567",
        })
        self.ok("review_result", {
            "project_id": project_id, "ticket_id": alias, "attempt_id": attempt,
            "submission_id": submission["submission_id"], "reviewer_session_id": f"reviewer-{prefix}",
            "verdict": "accept", "reason": "通过",
            "checks": [{"id": "C1", "passed": True, "evidence": "核对了"}],
        })
        self.ok("record_notice", {
            "project_id": project_id, "ticket_id": alias, "attempt_id": attempt,
            "session_id": f"coordinator-{prefix}", "origin_session_id": f"coordinator-{prefix}",
            "status": "sent", "detail": "已把回执发回原会话",
            "target_session_id": f"coordinator-{prefix}", "wake_kind": "queued",
        })
        self.ok("merge_ticket", {"project_id": project_id, "ticket_id": alias,
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

    # -- 轮次属于哪个项目，就按那个项目判（AW-158 C2）-------------------------

    def test_list_does_not_flag_a_round_that_belongs_to_another_project(self):
        """本项目有已合并的单，但那一轮属于别的项目：本项目不为它报「未收尾」。

        以前 `--list` 只看当前检出的 `tasks/round.md`，拿**本项目**的在途集合去套
        别的项目的轮：本项目的账已经清了，于是把一个不归它的轮报成「该收没收」。
        """
        self.land_and_merge()
        other = self.other_project()
        self.set_status("进行中", project="other")
        self.write_other_round(other, "已收尾")
        code, out, err = self.list_command()
        self.assertEqual(0, code, out + err)
        self.assertNotIn("本轮 R-900 未收尾", out + err)
        # 不只要「没红」：要说清那一轮在那边已经收了、本项目不为它报。
        self.assertIn("已收尾", err)
        self.assertIn("不为它报", err)

    def test_list_flags_an_unclosed_round_of_the_project_it_names(self):
        """另一种误判：轮次属于别的项目、那边真的该收尾——照样报，并指向那边的落点。

        漏判和误判是同一处错的两面：不按项目判时，别人的账本项目永远不会举起来。
        状态与落点必须来自**同一个文件**：报里叫你去改的那份，就是承载这个
        `状态：进行中` 的那份。
        """
        other = self.other_project()
        create_numbered(self.ok, self.ids, "other-1", {
            "project_id": "other", "title": "那边的成果", "state": "planned",
            "deliverable": "那边的交付物",
            "prd_path": "PRD/PRD-001-示例.md",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        })
        self.land_and_merge(project_id="other", alias="other-1", root=other, prefix="other")
        self.set_status("进行中", project="other")
        self.write_other_round(other, "进行中")
        code, out, err = self.list_command()
        self.assertEqual(1, code, out + err)
        text = out + err
        self.assertIn("R-900", text)
        self.assertIn("other", text)
        # 指向那个项目自己的落点（排除 `仓库\tasks\…` 那种本项目的路径）；
        # 临时目录在 Windows 上会缩短成 8.3 名，所以只比后半段。
        self.assertIn(os.path.join("别的项目", "tasks", "closing-r-900.md"), os.path.normcase(text))
        # 叫你去改的那份 round.md 是**那个项目**的，不是当前检出这一份。
        self.assertIn(os.path.join("别的项目", "tasks", "round.md"), os.path.normcase(text))
        self.assertIn("收尾再开新单", text)

    def test_list_notes_a_round_number_mismatch_instead_of_judging_it(self):
        """两边轮次对不上时只出旁注：既不判也不替谁改，更不能拿一边的状态去报另一边。"""
        other = self.other_project()
        self.set_status("进行中", project="other")
        self.write_other_round(other, "进行中", number="R-901")
        code, out, err = self.list_command()
        self.assertEqual(0, code, out + err)
        self.assertIn("R-901", err)
        self.assertIn("对不上", err)
        self.assertNotIn("本轮 R-900 未收尾", out + err)

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


if __name__ == "__main__":
    unittest.main()
