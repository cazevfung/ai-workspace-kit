"""安装面体检：一个已登记项目「装好了没有」，要有一条能当场判红的命令。

PRD-013 R5 的要求只有一句能当判据的东西：缺项存在时退出码 1、逐项给补法；全齐时
退出码 0。所以这里的用例断的是**退出码**与**逐项都在**，不断言措辞——改一个字
不该让用例变红，少报一项必须变红。

`project_doctor` 只读：它不改任何仓库的 `core.hooksPath`、不写 `.gitignore`，
验收时也按这个口径看（用例里跑一遍再核对配置没被动过）。
"""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import project_doctor
import work_gate


def git(repo, *args, check=True):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败：{result.stderr}")
    return result


class ProjectDoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        # 合规仓库：钩子指向本仓库 .githooks、.worktrees 已忽略、根上有 AGENTS.md。
        self.good = self.root / "合规仓库"
        self.good.mkdir()
        git(self.good, "init", "-q")
        (self.good / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
        (self.good / "AGENTS.md").write_text("开工说明\n", encoding="utf-8")
        git(self.good, "config", "core.hooksPath", str(self.good / ".githooks"))

        # 缺项仓库：三项全缺。
        self.bad = self.root / "缺项仓库"
        self.bad.mkdir()
        git(self.bad, "init", "-q")

        # 非 git 项目：只报「不是仓库」，不算失败。
        self.plain = self.root / "文档目录"
        self.plain.mkdir()
        (self.plain / "AGENTS.md").write_text("开工说明\n", encoding="utf-8")

        self.db = self.root / "center.sqlite3"
        self.counter = 0
        self.ok("init", {})
        for project_id, path in (("good", self.good), ("bad", self.bad), ("plain", self.plain)):
            self.ok("register_project", {"project_id": project_id, "name": project_id, "root": str(path)})

    def ok(self, operation, payload):
        self.counter += 1
        result = work_gate.load_center()(self.db).execute(operation, payload, request_id=f"req-{self.counter}")
        self.assertTrue(result["ok"], result)
        return result["result"]

    def run_doctor(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = project_doctor.main(["project_doctor.py", *args])
        return code, out.getvalue(), err.getvalue()

    def test_missing_items_exit_one_and_each_one_says_how_to_fix(self):
        code, out, err = self.run_doctor("--db", str(self.db))
        self.assertEqual(1, code, out + err)
        text = out + err
        self.assertIn(str(self.bad.resolve()), text)
        # 三项缺项各自要点名，且各自后面跟一句可以照做的补法（不是只有项目名）。
        self.assertIn("core.hooksPath", text)
        self.assertIn("git -C", text)
        self.assertIn(".worktrees", text)
        self.assertIn(".gitignore", text)
        self.assertIn("AGENTS.md", text)

    def test_json_output_is_machine_readable(self):
        code, out, err = self.run_doctor("--db", str(self.db), "--json")
        self.assertEqual(1, code, out + err)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        by_id = {item["project_id"]: item for item in payload["result"]["projects"]}
        self.assertEqual(set(), set(by_id["good"]["problems"]))
        self.assertTrue(by_id["bad"]["problems"])
        self.assertFalse(by_id["bad"]["hooks_ok"])
        self.assertFalse(by_id["bad"]["worktrees_ignored"])
        self.assertFalse(by_id["bad"]["docs_found"])
        # 非 git 项目：钩子与 .gitignore 不检查，也不算缺项。
        self.assertFalse(by_id["plain"]["is_git"])
        self.assertEqual([], by_id["plain"]["problems"])

    def test_not_a_repository_is_reported_but_not_a_failure(self):
        plain_db = self.root / "plain.sqlite3"
        self.counter = 0
        # 只登记那一个非 git 目录：全齐时退出码必须是 0。
        original = self.db
        self.db = plain_db
        try:
            self.ok("init", {})
            self.ok("register_project", {"project_id": "plain", "name": "plain", "root": str(self.plain)})
        finally:
            self.db = original
        code, out, err = self.run_doctor("--db", str(plain_db))
        self.assertEqual(0, code, out + err)
        self.assertIn("不是仓库", out + err)

    def test_a_compliant_repository_exits_zero(self):
        """C5 的第二半：临时造的合规仓库退 0。"""
        code, out, err = self.run_doctor("--root", str(self.good))
        self.assertEqual(0, code, out + err)

    def test_a_missing_docs_file_alone_fails_a_compliant_repository(self):
        """只缺 AGENTS.md 也必须红：不然「看着合规」就等于合规。"""
        (self.good / "AGENTS.md").unlink()
        code, out, err = self.run_doctor("--root", str(self.good))
        self.assertEqual(1, code, out + err)
        self.assertIn("AGENTS.md", out + err)

    def test_the_doctor_never_changes_repository_configuration(self):
        """只读：跑完 hooksPath 与 .gitignore 一字不变。"""
        before = git(self.bad, "config", "core.hooksPath", check=False).stdout
        self.run_doctor("--db", str(self.db))
        self.assertEqual(before, git(self.bad, "config", "core.hooksPath", check=False).stdout)
        self.assertFalse((self.bad / ".gitignore").exists(), "医生不许替人写 .gitignore")


if __name__ == "__main__":
    unittest.main()
