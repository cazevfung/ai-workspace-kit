"""按项目配置的用例：add_project 写什么、auto_merge 缺配置时怎么停。

C5 要证明的不是「能读到 JSON」，而是缺配置时**不回落**到任何默认测试命令：
以前 `auto_merge` 固定跑 `tools/run_tests.py`，项目没接进工作区也照样跑出一行
「全部通过」；现在必须先有 `.agents/ticket-config.json`，缺失就在第 2 步停住并
指向 `add_project.py`。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class MissingConfigTest(RepoCase):
    def test_load_project_config_names_add_project(self):
        with self.assertRaises(auto_merge.Fail) as caught:
            auto_merge.load_project_config(self.repo)
        self.assertEqual(auto_merge.STEP_CONFIG, caught.exception.step)
        self.assertIn("add_project.py", caught.exception.fix)

    def test_run_merge_stops_before_any_test_or_git_write(self):
        tip = self.git("rev-parse", "HEAD").stdout.strip()
        ticket = {
            "id": "AW-901", "project_id": "p-test", "title": "缺配置",
            "state": "done", "merges": [],
            "notices": [{"status": "sent", "detail": "fixture"}],
            "submissions": [{"head_sha": tip}],
        }
        out = []
        with mock.patch.object(auto_merge, "run_tests_in") as tests:
            with self.assertRaises(auto_merge.Fail) as caught:
                auto_merge.run_merge(
                    self.repo, "AW-901", branch="ticket/AW-901", target="master",
                    ticket_reader=lambda _: ticket, merge_recorder=lambda payload: payload,
                    out=out.append, env=self.env,
                )
        tests.assert_not_called()
        self.assertEqual(auto_merge.STEP_CONFIG, caught.exception.step)
        self.assertIn("add_project.py", caught.exception.fix)
        # 目标分支与配置都没动：失败发生在任何 git 写入之前。
        self.assertFalse(self.config_path().exists())
        self.assertEqual(tip, self.git("rev-parse", "master").stdout.strip())

    def test_main_exits_non_zero_and_points_at_add_project(self):
        """C5 的字面口径：缺配置时命令行非零退出，并提示跑 add_project。"""
        out = []
        code = auto_merge.main(["--ticket", "AW-901", "--repo", str(self.repo)], out=out.append)
        text = "\n".join(out)
        self.assertEqual(1, code, text)
        self.assertIn("add_project.py", text)
        self.assertIn(auto_merge.STEP_CONFIG, text)

    def test_config_without_test_cmd_is_rejected(self):
        self.write(".agents/ticket-config.json", json.dumps({"target_branch": "master"}))
        with self.assertRaises(auto_merge.Fail) as caught:
            auto_merge.load_project_config(self.repo)
        self.assertIn("test_cmd", caught.exception.reason)

    def test_config_without_target_branch_is_rejected(self):
        self.write(".agents/ticket-config.json", json.dumps({"test_cmd": ["python", "-c", "pass"]}))
        with self.assertRaises(auto_merge.Fail) as caught:
            auto_merge.load_project_config(self.repo)
        self.assertIn("target_branch", caught.exception.reason)

    def test_run_tests_in_refuses_an_empty_test_cmd(self):
        with self.assertRaises(auto_merge.Fail) as caught:
            auto_merge.run_tests_in(self.repo, python=sys.executable, test_cmd=[], env=self.env)
        self.assertIn("test_cmd", caught.exception.reason)

    def test_config_round_trip_through_add_project(self):
        add_project.add_project(self.repo, test_cmd="python -c pass")
        config = auto_merge.load_project_config(self.repo)
        self.assertEqual(["python", "-c", "pass"], config["test_cmd"])
        self.assertEqual("master", config["target_branch"])


if __name__ == "__main__":
    unittest.main()
