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

import hashlib
import json
import os
import re
import shutil
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

# 安装器末尾那份总结的两个固定写法（AWK-119）。改措辞会先红在这里——那正是我们要的：
# 这两行是「装完看得清」的全部内容，被人顺手改写而没人发现，等于这次修的洞又开了。
SKIP_LINE_PREFIX = "未写："
SKIP_REASON = "默认不写用户级配置，加 --allow-user-config"
TOTAL_LINE = re.compile(r"本次(?:装了|会装) (\d+) 项、跳过 (\d+) 项")
DONE_LINE = re.compile(r"已装 (\d+) 项：(.+)")


def summary_counts(output: str):
    """从 install.py 的输出里取出 (装了 N 项, 跳过 M 项, 逐条「未写：」行)。

    合计与逐条必须是同一份账：这里不信任代码里的计数器，直接把屏幕上的两处
    取出来比一次。「数得清」本身就是要交付的东西，自述不算。
    找不到合计那一行时返回 (None, None, 行)，调用方据此报红。
    """
    total = TOTAL_LINE.search(output)
    lines = [line.strip() for line in output.splitlines() if line.strip().startswith(SKIP_LINE_PREFIX)]
    if total is None:
        return None, None, lines
    return int(total.group(1)), int(total.group(2)), lines


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

    # 沙箱安装默认不写的那四个用户级位置（AWK-113 的判据，AWK-119 只负责说出来）。
    def user_level_paths(self):
        return (
            self.agents / "skills",
            self.agents / "center.json",
            self.ws / ".agents" / "center.json",
            self.agents / "intake",
            self.agents / ".ai-workspace-kit.json",
        )

    def test_sandbox_install_summary_lists_skips_and_totals(self):
        """不带开关装到临时目录：总结里数得清跳过了哪几项，合计对得上逐条。

        现场是 2026-11-24 03:38 的演练：退出码 0、屏幕上只有「安装完成」，而 27 个技能
        一个都没装。只断言「屏幕上出现过某个词」不够——那句跳过夹在 12 个步骤中间，
        所以这里要求三件事同时成立：逐条带路径与原因、合计与逐条行数相等、装了 N 项
        那行真的列了 N 个名字。
        """
        result = self.run_install("--workspace", str(self.ws),
                                  "--agents-home", str(self.agents), "--no-git")
        output = result.stdout + result.stderr
        self.assertEqual(0, result.returncode, output[-2000:])

        installed, skipped, lines = summary_counts(output)
        self.assertIsNotNone(installed, "总结里没有「本次装了 N 项、跳过 M 项」这一行")
        self.assertEqual(len(lines), skipped, "合计里的跳过数与逐条「未写：」行数对不上")
        self.assertGreaterEqual(skipped, 5, "沙箱默认该跳过的五个用户级位置没数全")

        joined = "\n".join(lines)
        for path in self.user_level_paths():
            # 路径与原因在同一行：只有路径没有原因，读的人不知道是「不需要」还是「有开关」。
            matched = [line for line in lines if str(path) in line]
            self.assertTrue(matched, f"跳过的路径没写出来：{path}")
            self.assertIn(SKIP_REASON, matched[0], f"跳过的行没写清原因：{matched[0]}")

        # 装了 N 项也要数得出来：那一行列了几个名字，N 就是几。
        done = DONE_LINE.search(output)
        self.assertIsNotNone(done, "总结里没有「已装 N 项：…」这一行")
        names = [name for name in done.group(2).strip().split("、") if name.strip()]
        self.assertEqual(int(done.group(1)), len(names), "合计里的已装数与列出的项名个数对不上")
        self.assertEqual(int(done.group(1)), installed)

        # 仍然一个用户级文件都没写：这一半由 install_guard 的用例与 verify_install 守着，
        # 这里再确认一次「说的是真的」。
        self.assertFalse((self.agents / "center.json").exists())
        self.assertFalse((self.agents / "skills").exists())
        self.assert_real_agents_untouched()

    def test_explicit_flag_summary_has_no_user_config_skip_lines(self):
        """带 --allow-user-config 时，用户级那几项不再进「未写：」——否则是噪声。

        反例正是本单要避免的另一种错：两种情形打同一套话，读的人分不出这次到底写了没写。
        这里不看合计里那个 M（--no-git 仍会跳过两个 git 项），只看用户级那四个路径。
        """
        result = self.run_install("--workspace", str(self.ws),
                                  "--agents-home", str(self.agents),
                                  "--allow-user-config", "--no-git")
        output = result.stdout + result.stderr
        self.assertEqual(0, result.returncode, output[-2000:])

        installed, skipped, lines = summary_counts(output)
        self.assertIsNotNone(installed, "总结里没有「本次装了 N 项、跳过 M 项」这一行")
        self.assertEqual(len(lines), skipped, "合计里的跳过数与逐条「未写：」行数对不上")

        joined = "\n".join(lines)
        for path in self.user_level_paths():
            self.assertNotIn(str(path), joined, f"带开关时不该再把它报成跳过：{path}")
        for line in lines:
            self.assertNotIn(SKIP_REASON, line, f"带开关时不该再出现「默认不写」的原因：{line}")

        # 反过来确认这次真的写了：没写而「不报跳过」才是最难查的那种。
        self.assertTrue((self.agents / "center.json").is_file())
        self.assertTrue((self.agents / "skills").is_dir())
        self.assertTrue((self.agents / "intake").is_dir())
        self.assertTrue((self.agents / ".ai-workspace-kit.json").is_file())
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


@unittest.skipUnless(INSTALL.is_file(), "装出来的工作区里没有仓库根的 install.py")
class GitattributesInstallTest(unittest.TestCase):
    """装出来的工作区里必须有仓库根那份 `.gitattributes`（AWK-120）。

    现场：2026-11-24 06:18 从 `git archive master` 解压装一遍，装出来的工作区里
    `python tools/run_tests.py` 约 3 项——`test_eol_check` 的 `FreshCloneTest` 两条
    与 `MissingRuleTest` 一条。原因是 `install.py` 只装 `kit/` 那棵树，仓库根的
    `.gitattributes` 没跟着走，346 个受管文件报「eol 属性是 unspecified」，
    而 README 说装完应全绿色。

    三件事分开验，因为它们不是同一件事：文件在、字节相同、规则真的生效。
    第三条只能拿装出来的工作区自己跑一次 `test_eol_check` 证明——
    把几条 eol 用例 skip 掉也能让 `run_tests` 变绿，那不是修好。
    用例只断言「装出来的那棵树能不能过」，不碰 `check_eol` 的判据与 `.gitattributes` 的内容。
    """

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        # resolve()：Windows 上临时目录名可能是 8.3 短名，install.py 打印的是长名。
        cls.root = Path(tmp.name).resolve()
        cls.ws = cls.root / "ws"
        cls.agents = cls.root / "agents"
        cls.git_available = shutil.which("git") is not None
        cls.git_config = cls.root / "gitconfig"
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        args = [sys.executable, str(INSTALL), "--workspace", str(cls.ws),
                "--agents-home", str(cls.agents), "--no-skills"]
        if cls.git_available:
            # 首次提交要 user.name / user.email，而这条用例不能依赖本机的全局配置：
            # 用一份临时 gitconfig 顶上，真实的 ~/.gitconfig 一个字节都不动。
            cls.git_config.write_text(
                "[user]\n\tname = AWK Test\n\temail = awk@example.com\n", encoding="utf-8")
            env["GIT_CONFIG_GLOBAL"] = str(cls.git_config)
        else:
            args.append("--no-git")
        cls.result = subprocess.run(args, cwd=str(REPO), capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", env=env, timeout=600)
        cls.output = cls.result.stdout + cls.result.stderr

    def test_installed_file_is_byte_identical(self):
        """文件在，且与仓库根那份同一个 sha256——不是一份长得像的副本。"""
        self.assertEqual(0, self.result.returncode, self.output[-2000:])
        source = REPO / ".gitattributes"
        target = self.ws / ".gitattributes"
        self.assertTrue(source.is_file(), f"套件根没有 {source}，这条用例失去意义")
        self.assertIn("行尾声明", self.output, "屏幕上看不出装没装这份文件")
        self.assertTrue(target.is_file(),
                        "装出来的工作区里没有 .gitattributes：受管文本的 eol 声明没跟过去")
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),
                         hashlib.sha256(target.read_bytes()).hexdigest(),
                         "装过去的 .gitattributes 与套件根那份不是同一份字节")

    def test_installed_workspace_eol_checks_are_green(self):
        """在装出来的工作区里跑那两组 eol 用例：必须真跑、真绿、不被 skip。"""
        if not self.git_available:
            self.skipTest("这台机器上没有 git，装出来的工作区里验不了行尾规则")
        self.assertEqual(0, self.result.returncode, self.output[-2000:])
        # 在 <工作区>/tools 下跑：那正是 `run_tests.py` 发现这两组用例的方式
        # （discover 把 start dir 放上 sys.path），且 `check_eol` 就在同一个目录。
        process = subprocess.run(
            [sys.executable, "-m", "unittest",
             "test_eol_check.FreshCloneTest", "test_eol_check.MissingRuleTest"],
            cwd=str(self.ws / "tools"), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=900)
        output = process.stdout + process.stderr
        self.assertEqual(0, process.returncode, output[-3000:])
        # 四条：FreshCloneTest 三条 + MissingRuleTest 一条。数目不对时不能说「绿了」——
        # 少跑一条和修好一条在屏幕上都是 OK。
        self.assertIn("Ran 4 tests", output, output[-3000:])
        self.assertIn("OK", output, output[-3000:])
        self.assertNotIn("skipped", output,
                         "eol 用例被跳过了：红不是被修的，是被藏起来的")


if __name__ == "__main__":
    unittest.main()
