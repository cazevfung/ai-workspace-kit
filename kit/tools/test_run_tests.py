"""tools/run_tests.py 自己的用例：防「没跑到」的几种样子。

AW-016 验收时发现：目录不存在时 discover 返回空 suite，于是把 tests/ 改名，
run_tests.py 会打印「=== tests/ (0 项) ===」然后退出码 0 报「全部通过」。
它防得住「用例红了没跑」，防不住「目录没了没跑」。下一张单的 auto_merge 要拿
它当闸门，所以这里把洞的三种入口都钉住：目录缺失、发现 0 项、有目录未声明进 SUITES。

AW-021 又把自检的两种误报也钉住，它们同样会让闸门误拦：已声明 suite 之下带
`__init__.py` 的包子目录（discover 真会跑到）被当成未声明；`.agents` 下属于另一条
开发线的脚本测试被当成本仓库漏跑。

用例全部用临时目录跑真实流程（run_suites 收 root 与 suites 作参数），
不依赖仓库当前的测试数量，也不会因为改仓库文件而变红。
"""
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_tests


PASSING = """import unittest


class Ok(unittest.TestCase):
    def test_ok(self):
        self.assertTrue(True)
"""


class RunSuitesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, relpath, text=PASSING):
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def suites(self):
        return (
            ("tests/", self.root / "tests", "test_*.py"),
            ("tools/", self.root / "tools", "test_*.py"),
        )

    def run_it(self):
        stream = io.StringIO()
        code = run_tests.run_suites(self.root, self.suites(), verbosity=0, stream=stream)
        return code, stream.getvalue()

    # C1：某一处的目录不存在
    def test_missing_suite_dir_fails(self):
        self.write("tests/test_alpha.py")
        code, out = self.run_it()
        self.assertEqual(1, code, out)
        # 必须指明是哪一处，而不是只写一句汇总
        self.assertIn("tools/ 声明的测试目录不存在", out)
        self.assertIn("目录缺失", out)
        self.assertIn("有失败", out)

    # C1：目录在，但发现到 0 项用例
    def test_zero_items_fails(self):
        self.write("tests/test_beta.py")
        self.write("tools/helper.py", "VALUE = 1\n")
        code, out = self.run_it()
        self.assertEqual(1, code, out)
        self.assertIn("tools/ 发现 0 项用例", out)
        self.assertIn("0 项", out)

    # C2：仓库里有 test_*.py 的目录没被 SUITES 声明
    def test_undeclared_test_dir_fails(self):
        self.write("tests/test_gamma.py")
        self.write("tools/test_delta.py")
        self.write("extra/test_epsilon.py")
        code, out = self.run_it()
        self.assertEqual(1, code, out)
        self.assertIn("extra/", out)
        self.assertIn("SUITES", out)
        self.assertIn("永远不会被跑到", out)

    # C2：跳过的目录不该被当成未声明
    def test_skipped_dirs_not_reported(self):
        self.write("tests/test_zeta.py")
        self.write("tools/test_eta.py")
        self.write(".worktrees/branch/tests/test_theta.py")
        self.write("_sandbox/test_iota.py")
        self.write("tools/__pycache__/test_kappa.py")
        code, out = self.run_it()
        self.assertEqual(0, code, out)
        self.assertIn("全部通过", out)

    def test_check_undeclared_suites_is_pure_over_root(self):
        self.write("tests/test_lambda.py")
        self.write("tools/test_mu.py")
        self.write("deep/nested/test_nu.py")
        self.assertEqual(
            ["deep/nested"],
            run_tests.check_undeclared_suites(self.root, ["tests", "tools"]),
        )
        # 只读：再查一次结果不变
        self.assertEqual(
            ["deep/nested"],
            run_tests.check_undeclared_suites(self.root, ["tests", "tools"]),
        )

    # C3：已声明 suite 之下带 __init__.py 的子目录，discover 会真跑到，不该报未声明
    def test_subpackage_under_suite_is_not_undeclared(self):
        self.write("tests/test_pi.py")
        self.write("tests/pkgok/__init__.py", "")
        self.write("tests/pkgok/test_rho.py")
        self.write("tools/test_sigma.py")
        code, out = self.run_it()
        self.assertEqual(0, code, out)
        self.assertIn("全部通过", out)
        # 统计到了才算真跑到，否则「不报未声明」可能只是又漏跑了
        self.assertIn("合计 3 项", out)
        self.assertNotIn("永远不会被跑到", out)
        self.assertNotIn("tests/pkgok", out)

    # C3：子目录没有 __init__.py 时 discover 确实进不去，仍要报未声明
    def test_non_package_subdir_still_reported(self):
        self.write("tests/test_tau.py")
        self.write("tests/plain/test_upsilon.py")
        self.write("tools/test_phi.py")
        code, out = self.run_it()
        self.assertEqual(1, code, out)
        self.assertIn("tests/plain/", out)
        self.assertIn("永远不会被跑到", out)

    # C3：包链断在中间那一层时，深处的子目录仍然跑不到
    def test_broken_package_chain_is_reported(self):
        self.write("tests/test_chi.py")
        self.write("tests/chain/__init__.py", "")
        self.write("tests/chain/deep/test_psi.py")
        self.write("tools/test_omega.py")
        code, out = self.run_it()
        self.assertEqual(1, code, out)
        self.assertIn("tests/chain/deep/", out)

    def test_subpackage_check_is_pure_over_root(self):
        self.write("tests/test_alpha1.py")
        self.write("tests/pkg/__init__.py", "")
        self.write("tests/pkg/test_beta1.py")
        self.write("tools/test_gamma1.py")
        self.assertEqual([], run_tests.check_undeclared_suites(self.root, ["tests", "tools"]))
        self.assertEqual([], run_tests.check_undeclared_suites(self.root, ["tests", "tools"]))

    # C4：.agents 下的脚本测试属于另一条开发线，不报未声明也不被统计
    def test_agents_scripts_are_skipped(self):
        self.write("tests/test_delta1.py")
        self.write("tools/test_epsilon1.py")
        # 故意写成会红的用例：真被跑到了，退出码就不会是 0
        failing = "import unittest\n\n\nclass Bad(unittest.TestCase):\n    def test_bad(self):\n        self.fail('should not run')\n"
        self.write(".agents/skills/skill-routing/scripts/test_check_routing.py", failing)
        code, out = self.run_it()
        self.assertEqual(0, code, out)
        self.assertIn("全部通过", out)
        self.assertIn("合计 2 项", out)
        self.assertNotIn(".agents", out)
        self.assertNotIn("should not run", out)

    def test_agents_dir_not_scanned(self):
        self.write(".agents/skills/x/scripts/test_zeta1.py")
        self.assertEqual([], run_tests.find_test_dirs(self.root))
        self.write("tests/test_eta1.py")
        self.assertEqual(["tests"], run_tests.find_test_dirs(self.root))

    # 正常情况
    def test_clean_repo_passes(self):
        self.write("tests/test_xi.py")
        self.write("tools/test_omicron.py")
        code, out = self.run_it()
        self.assertEqual(0, code, out)
        self.assertIn("全部通过", out)
        self.assertIn("合计 2 项", out)

    # 真实仓库自己也得过自检：新增测试目录忘了声明，这条会红
    def test_real_repo_has_no_undeclared_suite(self):
        declared = [path.relative_to(run_tests.ROOT).as_posix() for _, path, _ in run_tests.SUITES]
        self.assertEqual([], run_tests.check_undeclared_suites(run_tests.ROOT, declared))


GREEN_TESTS = '''import unittest


class AlphaCase(unittest.TestCase):
    def test_alpha_green(self):
        self.assertTrue(True)
'''

GREEN_TOOLS = '''import unittest


class BetaCase(unittest.TestCase):
    def test_beta_green(self):
        self.assertTrue(True)
'''

RED_TOOLS = '''import unittest


class BetaCase(unittest.TestCase):
    def test_beta_explodes(self):
        self.fail("故意写红的用例：tools 这一处必须整体非零")
'''


class ParallelRunTest(unittest.TestCase):
    """AW-110：两处用例改成两个子进程并行跑，但退出码语义与输出可读性不变。

    串行版（run_suites）保留给 --serial 排查用，用例上面那一批照旧跑它。
    这里钉的是并行版的四条：一处红整体红、输出按块不交错、汇总仍打两处
    用例数与合计、main 认 --serial 与默认并行。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, relpath, text=PASSING):
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def suites(self):
        return (
            ("tests/", self.root / "tests", "test_*.py"),
            ("tools/", self.root / "tools", "test_*.py"),
        )

    def run_parallel(self, verbosity=1):
        stream = io.StringIO()
        code = run_tests.run_suites_parallel(
            self.root, self.suites(), verbosity=verbosity, stream=stream
        )
        return code, stream.getvalue()

    # C2：一处红，整体就得非零，而且要能看出红在哪一处、哪一条
    def test_one_red_suite_makes_overall_red(self):
        self.write("tests/test_a.py", GREEN_TESTS)
        self.write("tools/test_b.py", RED_TOOLS)
        code, out = self.run_parallel()
        self.assertEqual(1, code, out)
        self.assertIn("test_beta_explodes", out)  # 是哪一条
        self.assertIn("故意写红的用例", out)  # 带理由的 traceback
        self.assertIn("tools/ 1 FAIL(1)", out)  # 是哪一处
        self.assertIn("tests/ 1 OK", out)  # 另一处不受牵连

    # C2：两处输出必须按块归属，不能你一句我一句
    def test_parallel_output_blocks_do_not_interleave(self):
        self.write("tests/test_a.py", GREEN_TESTS)
        self.write("tools/test_b.py", GREEN_TOOLS)
        code, out = self.run_parallel(verbosity=2)
        self.assertEqual(0, code, out)
        self.assertIn("test_alpha_green", out)
        self.assertIn("test_beta_green", out)
        tests_header = out.index("=== tests/")
        tools_header = out.index("=== tools/")
        self.assertLess(tests_header, tools_header, out)
        # tests 那一处的用例名必须全在 tools 的头之前出现过
        self.assertLess(out.index("test_alpha_green"), tools_header, out)
        # tools 那一处的用例名必须在自己的头之后
        self.assertGreater(out.index("test_beta_green"), tools_header, out)
        # 而 tests 那一处结束了就别再冒出来（块中不再有它）
        self.assertNotIn("test_alpha_green", out[tools_header:])

    # C1：汇总仍然分别打两处的用例数与合计
    def test_parallel_summary_keeps_both_counts(self):
        self.write("tests/test_a.py", GREEN_TESTS)
        self.write("tools/test_b.py", GREEN_TOOLS)
        code, out = self.run_parallel()
        self.assertEqual(0, code, out)
        self.assertIn("tests/ 1 OK", out)
        self.assertIn("tools/ 1 OK", out)
        self.assertIn("合计 2 项", out)
        self.assertIn("全部通过", out)

    # 并行版同样不能把「目录没了」当成 0 项通过
    def test_parallel_missing_suite_dir_fails(self):
        self.write("tests/test_a.py")
        code, out = self.run_parallel()
        self.assertEqual(1, code, out)
        self.assertIn("tools/ 声明的测试目录不存在", out)
        self.assertIn("目录缺失", out)

    # C5：main 默认并行，--serial 时退回串行
    def test_main_defaults_to_parallel_and_serial_is_opt_in(self):
        self.write("tests/test_a.py", GREEN_TESTS)
        self.write("tools/test_b.py", GREEN_TOOLS)
        stream = io.StringIO()
        code = run_tests.main([], root=self.root, suites=self.suites(), stream=stream)
        self.assertEqual(0, code, stream.getvalue())
        self.assertIn("并行模式", stream.getvalue())

        stream = io.StringIO()
        code = run_tests.main(["--serial"], root=self.root, suites=self.suites(), stream=stream)
        self.assertEqual(0, code, stream.getvalue())
        self.assertIn("全部通过", stream.getvalue())
        self.assertNotIn("并行模式", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
