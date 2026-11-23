"""按项目配置的用例：add_project 写什么、auto_merge 的测试命令按哪三层解。

判据从「缺配置就停」改成了「三层依次退」：合并工具的测试命令按
`tools/merge-projects.json`（工作区侧一张表）→ 目标仓库自己的
`.agents/ticket-config.json`（`add_project.py` 写的那一份）→ 缺省的
`python tools/run_tests.py` 解，三层都没有才报 `NO_TEST_COMMAND`。
改的原因是真实项目里两种配置都存在：新登记走表，老安装只写过自己那份文件；
把它们当成互斥的话，其中一边的测试命令会被静默忽略。

为什么不许「没配就空跑」：那会把「没跑测试」显示成「测试通过」。缺省这一层
不是可选项，它是闸门本身；缺省也跑不起来（仓库里没有 `tools/run_tests.py`）
时报的是 `NO_TEST_COMMAND`，不是「测试红」。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import add_project  # noqa: E402
import auto_merge  # noqa: E402


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "business"
        self.repo.mkdir()
        config = self.root / "gitconfig"
        config.write_text("", encoding="utf-8")
        self.env = {
            "GIT_AUTHOR_NAME": "project config test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "project config test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        self.git("init", "-b", "master")
        self.write("README.md", "base\n")
        self.git("add", "-A")
        self.git("commit", "-m", "base")

    def git(self, *args, cwd=None, check=True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=str(cwd or self.repo), check=check,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, **self.env},
        )

    def write(self, relpath, text):
        path = self.repo / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def config_path(self):
        return self.repo / ".agents" / "ticket-config.json"


class AddProjectTest(RepoCase):
    def test_writes_config_and_points_hooks_at_the_suite(self):
        result = add_project.add_project(self.repo, test_cmd='python -c "print(1)"')
        data = json.loads(self.config_path().read_text(encoding="utf-8"))
        self.assertEqual(Path(result["config_path"]).resolve(), self.config_path().resolve())
        self.assertEqual(["python", "-c", "print(1)"], data["test_cmd"])
        self.assertEqual("master", data["target_branch"])
        hooks = self.git("config", "core.hooksPath").stdout.strip()
        self.assertEqual((HERE.parent / ".githooks").resolve().as_posix(), hooks)

    def test_target_branch_can_be_overridden(self):
        add_project.add_project(self.repo, target_branch="main")
        data = json.loads(self.config_path().read_text(encoding="utf-8"))
        self.assertEqual("main", data["target_branch"])

    def test_existing_config_is_not_overwritten_without_force(self):
        add_project.add_project(self.repo)
        with self.assertRaises(add_project.AddProjectError):
            add_project.add_project(self.repo, target_branch="other")
        data = json.loads(self.config_path().read_text(encoding="utf-8"))
        self.assertEqual("master", data["target_branch"])
        add_project.add_project(self.repo, target_branch="other", force=True)
        data = json.loads(self.config_path().read_text(encoding="utf-8"))
        self.assertEqual("other", data["target_branch"])

    def test_dry_run_writes_nothing(self):
        add_project.add_project(self.repo, dry_run=True)
        self.assertFalse(self.config_path().exists())
        missing = self.git("config", "core.hooksPath", check=False)
        self.assertNotEqual(0, missing.returncode)

    def test_not_a_git_repo_stops_with_a_fix(self):
        plain = self.root / "plain"
        plain.mkdir()
        with self.assertRaises(add_project.AddProjectError) as caught:
            add_project.add_project(plain)
        self.assertIn("git init", str(caught.exception))


class TestCommandLayersTest(RepoCase):
    """三层解析：表格 → 仓库自己那份配置 → 缺省脚本。"""

    def test_no_config_at_all_means_the_default_script(self):
        plan = auto_merge.resolve_test_plan("p-test", repo=self.repo)
        self.assertEqual("default", plan.kind)
        self.assertIn("缺省", plan.describe())

    def test_ticket_config_supplies_the_command_when_the_table_has_no_entry(self):
        add_project.add_project(self.repo, test_cmd='python -c "print(1)"')
        plan = auto_merge.resolve_test_plan("p-test", repo=self.repo)
        self.assertEqual("command", plan.kind)
        self.assertEqual(["python", "-c", "print(1)"], plan.command)
        self.assertIn("ticket-config.json", plan.describe())

    def test_the_table_wins_over_the_repository_config(self):
        """表里登记了就以表为准：新的一层不该被老文件覆盖。"""
        add_project.add_project(self.repo, test_cmd='python -c "print(1)"')
        table = self.root / "merge-projects.json"
        table.write_text(json.dumps({"projects": {"p-test": {"test_command": ["node", "x.mjs"]}}}),
                         encoding="utf-8")
        plan = auto_merge.resolve_test_plan("p-test", config_path=table, repo=self.repo)
        self.assertEqual("command", plan.kind)
        self.assertEqual(["node", "x.mjs"], plan.command)

    def test_an_empty_test_cmd_in_the_repository_config_is_refused(self):
        self.write(".agents/ticket-config.json", json.dumps({"test_cmd": []}))
        with self.assertRaises(auto_merge.Fail) as caught:
            auto_merge.resolve_test_plan("p-test", repo=self.repo)
        self.assertEqual(auto_merge.MERGE_CONFIG_INVALID, caught.exception.code)
        self.assertIn("test_cmd", caught.exception.reason)

    def test_a_broken_repository_config_is_not_silently_ignored(self):
        self.write(".agents/ticket-config.json", "{不是 JSON")
        with self.assertRaises(auto_merge.Fail) as caught:
            auto_merge.resolve_test_plan("p-test", repo=self.repo)
        self.assertEqual(auto_merge.MERGE_CONFIG_INVALID, caught.exception.code)
        self.assertIn("ticket-config.json", caught.exception.reason)

    def test_default_target_prefers_the_repository_config_then_master(self):
        self.assertEqual("master", auto_merge.default_target(self.repo))
        add_project.add_project(self.repo, target_branch="main")
        self.assertEqual("main", auto_merge.default_target(self.repo))

    def test_missing_default_script_is_no_test_command_not_a_red_suite(self):
        plan = auto_merge.resolve_test_plan("p-test", repo=self.repo)
        with self.assertRaises(auto_merge.Fail) as caught:
            auto_merge.run_tests_in(self.repo, plan=plan, python=sys.executable, env=self.env)
        self.assertEqual(auto_merge.NO_TEST_COMMAND, caught.exception.code)
        self.assertIn("tools/run_tests.py", caught.exception.reason)

    def test_the_default_script_runs_when_the_repository_has_one(self):
        # 输出只写 ASCII：子进程的 stdout 按当前平台编码，用例不该被控制台编码左右。
        self.write("tools/run_tests.py", "print('汇总：business 1 OK')\nraise SystemExit(0)\n")
        plan = auto_merge.resolve_test_plan("p-test", repo=self.repo)
        passed, summary, output = auto_merge.run_tests_in(
            self.repo, plan=plan, python=sys.executable, env=self.env)
        self.assertTrue(passed, output)
        self.assertIn("business 1 OK", summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
