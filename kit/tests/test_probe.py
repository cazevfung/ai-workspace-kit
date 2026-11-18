import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from task_center import probe
from task_center.core import Center

HAS_GIT = shutil.which("git") is not None
OLD = "2020-01-01T00:00:00+00:00"


def make_git_repo(root, message="init", when=OLD):
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    for args in (["init", "-b", "main"], ["add", "-A"], ["commit", "-m", message]):
        subprocess.run(["git", "-C", str(root), *args], capture_output=True, env=env, timeout=60, check=False)


class ProbeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def report(self, **kwargs):
        kwargs.setdefault("roots", [str(self.base)])
        return probe.collect(**kwargs)["result"]

    def test_unmarked_directory_is_not_a_candidate(self):
        (self.base / "not-a-project" / "src").mkdir(parents=True)
        (self.base / "not-a-project" / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        result = self.report()
        self.assertEqual(0, result["counts"]["scanned"])
        self.assertEqual([], result["rank"])

    def test_marked_directory_is_a_candidate(self):
        project = self.base / "项目甲"
        project.mkdir()
        (project / "README.md").write_text("# 甲\n", encoding="utf-8")
        result = self.report()
        self.assertEqual(1, result["counts"]["scanned"])
        self.assertEqual(str(project), result["rank"][0]["root"])

    def test_empty_shell_is_dormant_and_flagged(self):
        shell = self.base / "空壳"
        (shell / "PRD").mkdir(parents=True)
        result = self.report()
        item = result["rank"][0]
        self.assertEqual("dormant", item["activity"])
        self.assertTrue(item["empty_shell"])
        self.assertIn("empty shell", item["signals"])

    def test_nested_candidate_is_not_ranked(self):
        parent = self.base / "外层"
        child = parent / "内层"
        child.mkdir(parents=True)
        (parent / "README.md").write_text("# 外层\n", encoding="utf-8")
        (child / "README.md").write_text("# 内层\n", encoding="utf-8")
        result = self.report()
        self.assertEqual([str(parent)], [item["root"] for item in result["rank"]])
        self.assertEqual(1, result["counts"]["nested"])

    def test_depth_limit_is_respected(self):
        deep = self.base / "一" / "二" / "三"
        deep.mkdir(parents=True)
        (deep / "README.md").write_text("# 三\n", encoding="utf-8")
        self.assertEqual(0, self.report(depth=1)["counts"]["scanned"])
        self.assertEqual(1, self.report(depth=3)["counts"]["scanned"])

    def test_rank_truncates_and_reports_what_was_omitted(self):
        for name in ("甲", "乙", "丙", "丁"):
            directory = self.base / name
            directory.mkdir()
            (directory / "README.md").write_text("# x\n", encoding="utf-8")
        result = self.report(limit=2)
        self.assertEqual(4, result["counts"]["ranked"])
        self.assertEqual(2, result["counts"]["returned"])
        self.assertEqual(2, result["counts"]["omitted"]["active"])

    def test_absent_database_is_reported_and_not_created(self):
        missing = self.base / "缺" / "center.sqlite3"
        result = self.report(db=str(missing))
        self.assertFalse(result["registry"]["available"])
        self.assertEqual("database absent", result["registry"]["reason"])
        self.assertFalse(missing.exists())

    def test_registered_root_that_disappeared_is_broken(self):
        db = self.base / "center.sqlite3"
        project = self.base / "实存"
        project.mkdir()
        (project / "README.md").write_text("# x\n", encoding="utf-8")
        Center(db).execute("init", {}, "probe-test-init")
        self.assertTrue(Center(db).execute("register_project", {"project_id": "p1", "name": "实存",
                                                                "root": str(project)}, "probe-test-reg")["ok"])
        result = self.report(db=str(db))
        self.assertEqual(1, len(result["rank"]))
        self.assertEqual("registered", result["rank"][0]["state"])

        shutil.rmtree(project)
        result = self.report(db=str(db))
        self.assertEqual([], result["rank"])
        self.assertEqual(1, result["counts"]["broken"])
        self.assertEqual(["registered root no longer exists"], result["broken"][0]["signals"])

    def test_registered_project_inside_another_registered_root_is_kept(self):
        """Regression: a workspace project containing projects used to swallow all of them,
        because suppression treated any listed ancestor as covering the child."""
        db = self.base / "center.sqlite3"
        outer = self.base / "外层工作区"
        inner = outer / "内层项目"
        inner.mkdir(parents=True)
        (outer / "README.md").write_text("# 外层", encoding="utf-8")
        (inner / "README.md").write_text("# 内层", encoding="utf-8")
        Center(db).execute("init", {}, "probe-nest-init")
        for pid, root in (("outer", outer), ("inner", inner)):
            Center(db).execute("register_project", {"project_id": pid, "name": pid, "root": str(root)},
                               f"probe-nest-{pid}")
        result = self.report(db=str(db))
        self.assertEqual({"outer", "inner"}, {item["project_id"] for item in result["rank"]})
        self.assertEqual(0, result["counts"]["nested"])

    def test_unregistered_duplicate_inside_a_registered_root_is_still_suppressed(self):
        db = self.base / "center.sqlite3"
        outer = self.base / "外层工作区"
        stray = outer / "顺手放的东西"
        stray.mkdir(parents=True)
        (outer / "README.md").write_text("# 外层", encoding="utf-8")
        (stray / "README.md").write_text("# 杂项", encoding="utf-8")
        Center(db).execute("init", {}, "probe-stray-init")
        Center(db).execute("register_project", {"project_id": "outer", "name": "outer", "root": str(outer)},
                           "probe-stray-reg")
        result = self.report(db=str(db))
        self.assertEqual(["outer"], [item.get("project_id") for item in result["rank"]])
        self.assertEqual(1, result["counts"]["nested"])

    def test_coordinator_is_reported_but_never_ranked(self):
        db = self.base / "center.sqlite3"
        project = self.base / "协调处"
        project.mkdir()
        (project / "README.md").write_text("# x\n", encoding="utf-8")
        Center(db).execute("init", {}, "probe-coord-init")
        Center(db).execute("register_project", {"project_id": "coord", "name": "协调处",
                                                "root": str(project)}, "probe-coord-reg")
        result = self.report(db=str(db), coordinator="coord")
        self.assertEqual([], result["rank"])
        self.assertEqual("coordinator", result["coordinator"]["role"])

    def test_coordinator_root_does_not_silence_its_neighbours(self):
        """The coordinator is never offered, so it must not suppress candidates beneath it."""
        db = self.base / "center.sqlite3"
        house = self.base / "老巢"
        inside = house / "住在里面的项目"
        inside.mkdir(parents=True)
        (house / "README.md").write_text("# 老巢", encoding="utf-8")
        (inside / "README.md").write_text("# 里面", encoding="utf-8")
        Center(db).execute("init", {}, "probe-house-init")
        Center(db).execute("register_project", {"project_id": "house", "name": "house", "root": str(house)},
                           "probe-house-reg")
        result = self.report(db=str(db), coordinator="house")
        self.assertEqual([str(inside)], [item["root"] for item in result["rank"]])
        self.assertEqual(0, result["counts"]["nested"])

    def test_git_timeout_is_unobserved_never_active(self):
        project = self.base / "超时"
        (project / "README.md").parent.mkdir(parents=True, exist_ok=True)
        (project / "README.md").write_text("# x\n", encoding="utf-8")
        (project / ".git").mkdir()
        with mock.patch.object(probe, "git", return_value=None):
            result = self.report()
        item = result["rank"][0]
        self.assertEqual("unobserved", item["activity"])
        self.assertIsNone(item["last_touch"])
        self.assertIn("git timed out", item["signals"])

    def test_database_is_opened_read_only_and_left_unchanged(self):
        db = self.base / "center.sqlite3"
        Center(db).execute("init", {}, "probe-ro-init")
        project = self.base / "只读"
        project.mkdir()
        (project / "README.md").write_text("# x\n", encoding="utf-8")
        Center(db).execute("register_project", {"project_id": "ro", "name": "只读",
                                                "root": str(project)}, "probe-ro-reg")
        before = db.read_bytes()
        self.report(db=str(db))
        self.assertEqual(before, db.read_bytes())


@unittest.skipUnless(HAS_GIT, "git is not installed")
class GitHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_git_history_wins_over_a_fresh_file(self):
        """The trap: a directory's own mtime cannot see edits made deep inside it.
        A project committed years ago but touched a minute ago must still read as old."""
        project = self.base / "老项目"
        make_git_repo(project, when=OLD)
        nested = project / "src" / "deep"
        nested.mkdir(parents=True)
        fresh = nested / "今天改的.py"
        fresh.write_text("x = 1\n", encoding="utf-8")
        self.assertLess(time.time() - fresh.stat().st_mtime, 60)

        result = probe.collect([str(self.base)])["result"]
        item = result["rank"][0]
        self.assertIn("git", item)
        self.assertEqual("dormant", item["activity"])
        self.assertTrue(item["last_touch"].startswith("2019-12-31") or item["last_touch"].startswith("2020-01-01"))
        self.assertNotIn("no git; bounded file walk", item["signals"])

    def test_recent_commit_reads_as_active(self):
        project = self.base / "新项目"
        make_git_repo(project, when=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
        result = probe.collect([str(self.base)])["result"]
        item = result["rank"][0]
        self.assertEqual("active", item["activity"])
        self.assertEqual(1, item["git"]["recent_commits"])


class HereTest(unittest.TestCase):
    """A session that is already somewhere needs no scanning to find out where."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def assertSamePath(self, expected, actual):
        # tempfile hands back an 8.3 short name while resolve() yields the long form.
        self.assertEqual(probe.folded(expected), probe.folded(actual))

    def test_git_root_is_found_from_a_deep_subdirectory(self):
        project = self.base / "仓库"
        (project / "src" / "deep").mkdir(parents=True)
        (project / ".git").mkdir()
        found = probe.detect_here(project / "src" / "deep", stop=self.base)
        self.assertSamePath(project, found["root"])
        self.assertEqual("git root", found["basis"])

    def test_git_root_beats_a_marked_subdirectory(self):
        project = self.base / "仓库"
        docs = project / "docs"
        docs.mkdir(parents=True)
        (project / ".git").mkdir()
        (docs / "README.md").write_text("# 文档", encoding="utf-8")
        self.assertSamePath(project, probe.detect_here(docs, stop=self.base)["root"])

    def test_marker_ancestor_is_used_without_git(self):
        project = self.base / "文档项目"
        inner = project / "章节"
        inner.mkdir(parents=True)
        (project / "README.md").write_text("# x", encoding="utf-8")
        found = probe.detect_here(inner, stop=self.base)
        self.assertSamePath(project, found["root"])
        self.assertEqual("project marker", found["basis"])

    def test_outside_any_project_reports_no_root(self):
        nowhere = self.base / "桌面" / "杂物"
        nowhere.mkdir(parents=True)
        found = probe.detect_here(nowhere, stop=self.base)
        self.assertIsNone(found["root"])
        self.assertSamePath(nowhere, found["cwd"])

    def test_registered_ancestor_wins_over_a_deeper_git_root(self):
        """A git worktree sits inside its registered project; both facts must survive."""
        db = self.base / "center.sqlite3"
        outer = self.base / "工作区"
        worktree = outer / ".worktrees" / "分支"
        worktree.mkdir(parents=True)
        (outer / "README.md").write_text("# x", encoding="utf-8")
        (worktree / ".git").mkdir()
        Center(db).execute("init", {}, "here-init")
        Center(db).execute("register_project", {"project_id": "ws", "name": "工作区",
                                                "root": str(outer)}, "here-reg")
        report = probe.look(start=str(worktree), db=str(db), stop=self.base)["result"]
        self.assertSamePath(worktree, report["here"]["root"])
        self.assertEqual("ws", report["here"]["registered"]["project_id"])


class CenterDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def make_center(self, name="center.sqlite3"):
        db = self.base / name
        Center(db).execute("init", {}, f"disc-init-{name}")
        return db

    def config(self, body):
        path = self.base / "center.json"
        path.write_text(body, encoding="utf-8")
        return path

    def test_config_file_locates_the_centre(self):
        db = self.make_center()
        found = probe.read_center(config=self.config(json.dumps({"db": str(db)})))
        self.assertTrue(found["available"])
        self.assertEqual(str(db), found["path"])

    def test_argument_beats_the_config_file(self):
        db = self.make_center()
        other = self.make_center("other.sqlite3")
        found = probe.read_center(db=str(other), config=self.config(json.dumps({"db": str(db)})))
        self.assertEqual(str(other), found["path"])
        self.assertEqual("argument", found["source"])

    def test_absent_config_and_missing_database_are_reported_not_created(self):
        missing = self.base / "nowhere" / "center.sqlite3"
        found = probe.read_center(config=self.config(json.dumps({"db": str(missing)})))
        self.assertFalse(found["available"])
        self.assertEqual("database missing at the configured path", found["reason"])
        self.assertFalse(missing.exists())

        unset = probe.read_center(config=self.base / "absent.json")
        self.assertEqual("no centre configured", unset["reason"])

    def test_unreadable_or_emptied_config_is_reported(self):
        self.assertEqual("configuration is unreadable",
                         probe.read_center(config=self.config("{not json"))["reason"])
        self.assertEqual("configuration has no db entry",
                         probe.read_center(config=self.config("{}"))["reason"])


class BriefTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_brief_is_a_fraction_of_the_full_report(self):
        for name in ("甲乙", "丙丁", "戊己", "庚辛"):
            directory = self.base / name
            directory.mkdir()
            (directory / "README.md").write_text("# x", encoding="utf-8")
        full = probe.look(roots=[str(self.base)], config=self.base / "absent.json")["result"]
        short = probe.brief({"ok": True, "result": full})["result"]
        self.assertLessEqual(len(json.dumps(short, ensure_ascii=False)),
                             len(json.dumps(full, ensure_ascii=False)) // 2)
        self.assertEqual(3, len(short["near"]))
        self.assertIn("here", short)


if __name__ == "__main__":
    unittest.main()
