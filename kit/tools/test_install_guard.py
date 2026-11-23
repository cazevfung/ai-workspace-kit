"""沙箱安装不许改写真实的用户级配置（AWK-113）。

现场：2026-11-23 22:10，一个验收会话把工作区装到临时目录，`--agents-home` 用了默认值
（真实的 `~/.agents`），安装器于是把真实 `~/.agents/center.json` 改指到沙箱的空库。
那一刻这台机器上所有工作区工具一起读到空库，报「0 张在途」。

这一组用例锁三件事，任何一条单看都成立却都不够：

1. **判定**：沙箱（agents-home 不是本机真实用户目录）默认一个用户级文件都不写；
   要写必须显式 `--allow-user-config`。
2. **真装重指**：agents-home 就是真实用户目录、而那里的 `center.json` 已经指向别的库时，
   默认拒绝并把原内容原样打印；给了开关才覆盖。
3. **快照真的能抓改动**：前后对比真实 `~/.agents` 靠 `tree_digest`，所以它必须能认出
   「改了一个字节」「多了一个文件」——不然第 1、2 条的对勾是空的。

用例本身**不写真实 `~/.agents`**：所有安装都跑在临时 `--agents-home` 或假 HOME 下，
「真实目录没被动过」只用装前装后两次快照证明，不先写进去再恢复。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import install_guard

# 源码树里 install.py 在仓库根（`kit/tools/` 往上两层）；装出来的工作区里没有它，
# 端到端那一组用例在这种布局下跳过——不假绿，也不假红。
REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "install.py"
REAL_AGENTS = Path.home() / ".agents"


def snapshot_real_agents():
    """真实用户级目录的快照。只读，不创建、不修改任何东西。"""
    return install_guard.tree_digest(REAL_AGENTS)


class UserConfigDecisionTest(unittest.TestCase):
    """判定矩阵：谁能写、为什么、要不要人拍板。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.real = self.home / ".agents"
        self.ws = self.root / "ws"
        self.ws_db = self.ws / ".data" / "task-center.sqlite3"

    def decide(self, agents_home, **kwargs):
        return install_guard.decide_user_config(agents_home, self.ws_db, home=self.home, **kwargs)

    def test_sandbox_agents_home_writes_nothing_by_default(self):
        agents = self.root / "sandbox-agents"
        decision = self.decide(agents)
        self.assertTrue(decision.sandbox)
        self.assertFalse(decision.allowed)
        self.assertEqual("sandbox-skipped", decision.reason)
        self.assertEqual("", decision.refusal)
        self.assertTrue(install_guard.real_agents_home(self.home).resolve()
                        == decision.real_agents_home.resolve())

    def test_sandbox_agents_home_writes_with_explicit_flag(self):
        agents = self.root / "sandbox-agents"
        decision = self.decide(agents, allow_user_config=True)
        self.assertTrue(decision.sandbox)
        self.assertTrue(decision.allowed)
        self.assertEqual("explicit-allow", decision.reason)

    def test_real_home_first_install_is_unchanged(self):
        """首次安装（center.json 不存在）行为不变：照写。"""
        self.real.mkdir(parents=True)
        decision = self.decide(self.real)
        self.assertFalse(decision.sandbox)
        self.assertTrue(decision.allowed)
        self.assertEqual("first-install", decision.reason)
        self.assertEqual("", decision.refusal)

    def test_real_home_same_db_is_allowed(self):
        self.real.mkdir(parents=True)
        (self.real / "center.json").write_text(
            json.dumps({"db": self.ws_db.as_posix()}), encoding="utf-8")
        decision = self.decide(self.real)
        self.assertTrue(decision.allowed)
        self.assertEqual("same-db", decision.reason)

    def test_real_home_other_db_is_refused_and_prints_original(self):
        self.real.mkdir(parents=True)
        other = (self.root / "other" / "task-center.sqlite3").as_posix()
        (self.real / "center.json").write_text(json.dumps({"db": other}), encoding="utf-8")
        decision = self.decide(self.real)
        self.assertFalse(decision.allowed)
        self.assertEqual("would-repoint", decision.reason)
        self.assertEqual(other, decision.existing_db)
        self.assertIn("center.json", decision.refusal)
        self.assertIn("--allow-user-config", decision.refusal)
        self.assertIn(other, decision.existing_text)

    def test_real_home_other_db_is_overwritten_with_explicit_flag(self):
        self.real.mkdir(parents=True)
        other = (self.root / "other" / "task-center.sqlite3").as_posix()
        (self.real / "center.json").write_text(json.dumps({"db": other}), encoding="utf-8")
        decision = self.decide(self.real, allow_user_config=True)
        self.assertTrue(decision.allowed)
        self.assertEqual("explicit-allow", decision.reason)
        self.assertEqual("", decision.refusal)

    def test_real_home_unreadable_center_is_refused_with_raw_text(self):
        self.real.mkdir(parents=True)
        (self.real / "center.json").write_text("not a json at all", encoding="utf-8")
        decision = self.decide(self.real)
        self.assertFalse(decision.allowed)
        self.assertEqual("", decision.existing_db)
        self.assertEqual("not a json at all", decision.existing_text)
        self.assertIn("--allow-user-config", decision.refusal)

    def test_path_forms_are_compared_after_normalising(self):
        """`db` 写成正斜杠与反斜杠是同一个库，不该被当成「指着别的库」。"""
        self.real.mkdir(parents=True)
        (self.real / "center.json").write_text(
            json.dumps({"db": str(self.ws_db)}), encoding="utf-8")
        decision = self.decide(self.real)
        self.assertTrue(decision.allowed)
        self.assertEqual("same-db", decision.reason)


class TreeDigestTest(unittest.TestCase):
    """快照要能抓到「改了一个字节」与「多了一个文件」——否则前后对比是空的。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "sub").mkdir()
        (self.root / "a.txt").write_text("hello", encoding="utf-8")
        (self.root / "sub" / "b.txt").write_text("world", encoding="utf-8")

    def test_same_tree_same_digest(self):
        self.assertEqual(install_guard.tree_digest(self.root), install_guard.tree_digest(self.root))

    def test_content_change_is_detected(self):
        before = install_guard.tree_digest(self.root)
        (self.root / "a.txt").write_text("hellp", encoding="utf-8")
        after = install_guard.tree_digest(self.root)
        self.assertEqual(["a.txt"], install_guard.diff_tree(before, after))

    def test_new_file_is_detected(self):
        before = install_guard.tree_digest(self.root)
        (self.root / "sub" / "c.txt").write_text("new", encoding="utf-8")
        after = install_guard.tree_digest(self.root)
        self.assertEqual(["sub/c.txt"], install_guard.diff_tree(before, after))

    def test_missing_root_is_not_an_empty_tree(self):
        missing = install_guard.tree_digest(self.root / "nope")
        empty = self.root / "empty"
        empty.mkdir()
        self.assertEqual({}, missing)
        self.assertNotEqual(missing, install_guard.tree_digest(empty))


@unittest.skipUnless(INSTALL.is_file(), "装出来的工作区里没有仓库根的 install.py")
class InstallRunTest(unittest.TestCase):
    """真跑 install.py：全部落在临时目录里，真实 ~/.agents 只读、只做前后快照。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # 取 resolve() 后的路径：Windows 上临时目录名可能是 8.3 短名（`202108~1`），
        # 而 install.py 打印的是 resolve() 之后的长名，两边直接比会假红。
        self.root = Path(tmp.name).resolve()
        self.ws = self.root / "ws"
        self.agents = self.root / "agents"
        self.before = snapshot_real_agents()

    def run_install(self, *args, env=None):
        run_env = dict(os.environ)
        run_env["PYTHONIOENCODING"] = "utf-8"
        if env:
            run_env.update(env)
        return subprocess.run([sys.executable, str(INSTALL), *args],
                              cwd=str(REPO), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=run_env, timeout=600)

    def assert_real_agents_untouched(self):
        after = snapshot_real_agents()
        changed = install_guard.diff_tree(self.before, after)
        self.assertEqual([], changed,
                         f"真实 ~/.agents 被改动了：{', '.join(changed[:5])}"
                         if changed else "")

    def test_sandbox_install_writes_no_user_level_file(self):
        self.agents.mkdir()  # 沙箱用户级目录事先存在，避免「因为目录不存在才没写」混淆
        result = self.run_install("--workspace", str(self.ws),
                                  "--agents-home", str(self.agents), "--no-git")
        output = result.stdout + result.stderr
        self.assertEqual(0, result.returncode, output[-2000:])
        self.assertFalse((self.agents / "center.json").exists(), "沙箱不该写 center.json")
        self.assertFalse((self.agents / "skills").exists(), "沙箱不该写技能")
        self.assertFalse((self.agents / "intake").exists(), "沙箱不该写 intake")
        self.assertFalse((self.agents / ".ai-workspace-kit.json").exists(), "沙箱不该写安装清单")
        self.assertIn("跳过", output)
        self.assertIn("--allow-user-config", output)
        # 工作区照常可用：自带库在、闸门读到它，不因为少写一份用户级配置就变成空壳。
        self.assertTrue((self.ws / ".data" / "task-center.sqlite3").is_file())
        gate = subprocess.run([sys.executable, "tools/work_gate.py", "--list"], cwd=str(self.ws),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120)
        gate_output = gate.stdout + gate.stderr
        self.assertEqual(0, gate.returncode, gate_output[-1500:])
        self.assertNotIn("DB_NOT_FOUND", gate_output)
        self.assert_real_agents_untouched()

    def test_explicit_flag_writes_user_level_config(self):
        result = self.run_install("--workspace", str(self.ws),
                                  "--agents-home", str(self.agents),
                                  "--allow-user-config", "--no-git")
        output = result.stdout + result.stderr
        self.assertEqual(0, result.returncode, output[-2000:])
        center = json.loads((self.agents / "center.json").read_text(encoding="utf-8"))
        self.assertEqual((self.ws / ".data" / "task-center.sqlite3").resolve(),
                         Path(center["db"]).resolve())
        pointer = json.loads((self.ws / ".agents" / "center.json").read_text(encoding="utf-8"))
        self.assertEqual(self.agents.resolve(), Path(pointer["agents_home"]).resolve())
        self.assertTrue((self.agents / "intake" / "intake.md").is_file())
        self.assertTrue((self.agents / ".ai-workspace-kit.json").is_file())
        self.assertTrue(any((self.agents / "skills").iterdir()))
        self.assert_real_agents_untouched()

    def test_real_home_repoint_refused_then_allowed_with_flag(self):
        fake_home = self.root / "fake-home"
        fake_agents = fake_home / ".agents"
        fake_agents.mkdir(parents=True)
        fake = {"USERPROFILE": str(fake_home), "HOME": str(fake_home)}
        probe = subprocess.run([sys.executable, "-c", "from pathlib import Path;print(Path.home())"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", env={**os.environ, **fake}, timeout=120)
        if Path(probe.stdout.strip() or self.root).resolve() != fake_home.resolve():
            self.skipTest("这台机器上改 USERPROFILE/HOME 挪不动 Path.home()，"
                          "没法在不碰真实 ~/.agents 的前提下测「真装重指」")
        other = (self.root / "other" / "task-center.sqlite3").as_posix()
        center = fake_agents / "center.json"
        center.write_text(json.dumps({"db": other}), encoding="utf-8")
        original = center.read_bytes()

        result = self.run_install("--workspace", str(self.ws),
                                  "--agents-home", str(fake_agents), "--no-git", env=fake)
        output = result.stdout + result.stderr
        self.assertEqual(2, result.returncode, output[-2000:])
        self.assertIn(other, output)
        self.assertIn("--allow-user-config", output)
        self.assertEqual(original, center.read_bytes(), "默认拒绝时一个字节都不该改")
        self.assertFalse(self.ws.exists(), "拒绝要发生在复制工作区之前")
        self.assertFalse((fake_agents / "skills").exists())

        result = self.run_install("--workspace", str(self.ws),
                                  "--agents-home", str(fake_agents),
                                  "--allow-user-config", "--no-git", env=fake)
        output = result.stdout + result.stderr
        self.assertEqual(0, result.returncode, output[-2000:])
        center = json.loads(center.read_text(encoding="utf-8"))
        self.assertEqual((self.ws / ".data" / "task-center.sqlite3").resolve(),
                         Path(center["db"]).resolve())
        self.assert_real_agents_untouched()

    def test_dry_run_forecasts_skips_and_writes(self):
        result = self.run_install("--workspace", str(self.ws),
                                  "--agents-home", str(self.agents), "--dry-run", "--no-git")
        output = result.stdout + result.stderr
        self.assertEqual(0, result.returncode, output[-2000:])
        self.assertIn("跳过", output)
        self.assertIn("--allow-user-config", output)
        self.assertFalse(self.agents.exists(), "预演不许建目录")
        self.assertFalse(self.ws.exists(), "预演不许建工作区")

        result = self.run_install("--workspace", str(self.ws), "--agents-home", str(self.agents),
                                  "--allow-user-config", "--dry-run", "--no-git")
        output = result.stdout + result.stderr
        self.assertEqual(0, result.returncode, output[-2000:])
        self.assertIn(str(self.agents / "center.json"), output)
        self.assertIn("skills", output)
        self.assertFalse(self.agents.exists())

    def test_dry_run_forecasts_the_refusal(self):
        fake_home = self.root / "fake-home"
        fake_agents = fake_home / ".agents"
        fake_agents.mkdir(parents=True)
        other = (self.root / "other" / "task-center.sqlite3").as_posix()
        (fake_agents / "center.json").write_text(json.dumps({"db": other}), encoding="utf-8")
        result = self.run_install("--workspace", str(self.ws), "--agents-home", str(fake_agents),
                                  "--dry-run", "--no-git",
                                  env={"USERPROFILE": str(fake_home), "HOME": str(fake_home)})
        output = result.stdout + result.stderr
        self.assertEqual(0, result.returncode, output[-2000:])
        self.assertIn(other, output)
        self.assertIn("--allow-user-config", output)
        self.assertFalse(self.ws.exists())


if __name__ == "__main__":
    unittest.main()
