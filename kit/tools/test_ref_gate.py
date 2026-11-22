"""`tools/ref_gate.py` 的用例：两类引用各有一正一反，反例必须先红。

分工：正反 fixture 在 `tests/fixtures/ref-gate/`，临时目录里的样本由用例自己
造。检查的是「退出码 + 输出里有没有那一行位置」，不是实现细节——闸门的存在
理由就是别人拿着退出码就能判，用例也按这个口径写。
"""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ref_gate

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "ref-gate"


def run(argv):
    """跑一次闸门，返回 (退出码, 标准输出)。"""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = ref_gate.main(argv)
    return code, buffer.getvalue()


def last_line(text):
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


class HelpTest(unittest.TestCase):
    def test_help_exits_zero_and_names_both_kinds(self):
        code, out = run(["--help"])
        self.assertEqual(code, 0)
        for word in ("line", "anchor", "--kinds", "--paths"):
            self.assertIn(word, out)

    def test_no_arguments_prints_help(self):
        code, out = run([])
        self.assertEqual(code, 0)
        self.assertIn("--paths", out)


class LineRefTest(unittest.TestCase):
    def test_good_fixture_passes(self):
        code, out = run(["--paths", str(FIXTURES / "good-cite.md"), "--kinds", "all"])
        self.assertEqual(code, 0)
        self.assertIn("跳过：裸文件名 0 处、绝对路径 0 处", last_line(out))

    def test_missing_file_fails_with_position_and_target(self):
        code, out = run(["--paths", str(FIXTURES / "bad-line.md"), "--kinds", "line"])
        self.assertEqual(code, 1)
        self.assertIn("bad-line.md", out)
        self.assertIn("tests/fixtures/ref-gate/missing.md", out)
        self.assertIn("不存在", out)

    def test_line_overflow_fails(self):
        code, out = run(["--paths", str(FIXTURES / "bad-line.md"), "--kinds", "line"])
        self.assertEqual(code, 1)
        self.assertIn("第 999 行超出该文件的行数", out)

    def test_blank_line_fails(self):
        code, out = run(["--paths", str(FIXTURES / "bad-line.md"), "--kinds", "line"])
        self.assertEqual(code, 1)
        self.assertIn("第 8 行是空行", out)

    def test_whitespace_only_line_counts_as_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "doc").mkdir()
            (root / "doc" / "target.md").write_text("one\n   \nthree\n", encoding="utf-8")
            (root / "doc" / "cite.md").write_text("看 `doc/target.md:2` 那一行。\n", encoding="utf-8")
            code, out = run(
                ["--paths", "doc/cite.md", "--kinds", "line", "--root", str(root)]
            )
            self.assertEqual(code, 1)
            self.assertIn("是空行", out)


class AnchorTest(unittest.TestCase):
    def test_missing_heading_fails_with_position_and_title(self):
        code, out = run(["--paths", str(FIXTURES / "bad-anchor.md"), "--kinds", "anchor"])
        self.assertEqual(code, 1)
        self.assertIn("bad-anchor.md", out)
        self.assertIn("这一节还不存在", out)

    def test_existing_heading_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "doc").mkdir()
            (root / "doc" / "target.md").write_text("# 用法\n\n正文\n", encoding="utf-8")
            (root / "doc" / "cite.md").write_text(
                "见 `doc/target.md` 的「用法」。\n", encoding="utf-8"
            )
            code, out = run(
                ["--paths", "doc/cite.md", "--kinds", "anchor", "--root", str(root)]
            )
            self.assertEqual(code, 0, out)

    def test_title_in_plain_text_is_not_a_heading(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "doc").mkdir()
            (root / "doc" / "target.md").write_text("用法\n\n正文\n", encoding="utf-8")
            (root / "doc" / "cite.md").write_text(
                "见 `doc/target.md` 的「用法」。\n", encoding="utf-8"
            )
            code, out = run(
                ["--paths", "doc/cite.md", "--kinds", "anchor", "--root", str(root)]
            )
            self.assertEqual(code, 1)
            self.assertIn("标题「用法」不存在", out)


class SkipTest(unittest.TestCase):
    def test_bare_filename_is_skipped_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "core.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
            (root / "cite.md").write_text("看 `core.py:3` 那一行。\n", encoding="utf-8")
            code, out = run(["--paths", "cite.md", "--kinds", "line", "--root", str(root)])
            self.assertEqual(code, 0, out)
            self.assertIn("裸文件名 1 处", last_line(out))

    def test_absolute_path_is_skipped_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cite.md").write_text(
                "看 `C:\\Python314\\Lib\\json\\encoder.py:263` 那一行。\n", encoding="utf-8"
            )
            code, out = run(["--paths", "cite.md", "--kinds", "line", "--root", str(root)])
            self.assertEqual(code, 0, out)
            self.assertIn("绝对路径 1 处", last_line(out))


class KindsTest(unittest.TestCase):
    def test_line_kind_ignores_anchor_refs(self):
        code, out = run(["--paths", str(FIXTURES / "bad-anchor.md"), "--kinds", "line"])
        self.assertEqual(code, 0, out)

    def test_anchor_kind_ignores_line_refs(self):
        code, out = run(["--paths", str(FIXTURES / "bad-line.md"), "--kinds", "anchor"])
        self.assertEqual(code, 0, out)

    def test_kinds_accepts_comma_separated(self):
        code, out = run(["--paths", str(FIXTURES / "bad-line.md"), "--kinds", "line,anchor"])
        self.assertEqual(code, 1, out)

    def test_unknown_kind_is_usage_error(self):
        code, _ = run(["--paths", str(FIXTURES / "good-cite.md"), "--kinds", "url"])
        self.assertEqual(code, 2)


class UsageTest(unittest.TestCase):
    def test_missing_path_is_usage_error(self):
        code, _ = run(["--paths", "tests/fixtures/ref-gate/nope.md", "--kinds", "line"])
        self.assertEqual(code, 2)

    def test_unknown_flag_is_usage_error(self):
        code, _ = run(["--nope"])
        self.assertEqual(code, 2)


class EmptyTest(unittest.TestCase):
    """一处也没核到时不能与「核了很多处、都成立」长得一样。

    退 0 不变（没东西可核不是错），但 stdout 要有一行 `EMPTY`。以前两者都只是
    「引用闸门通过：核了 0 处引用」/「核了 N 处引用」，看起来一样。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_no_references_prints_empty_and_still_exits_zero(self):
        sample = self.root / "plain.md"
        sample.write_text("# 无引用的文档\n\n这里没有任何引用。\n", encoding="utf-8")
        code, out = run(["--paths", str(sample), "--kinds", "all"])
        self.assertEqual(code, 0, out)
        self.assertIn("核了 0 处引用", out)
        self.assertIn("EMPTY", out, f"核到 0 处时必须打出 EMPTY，实际输出：{out}")

    def test_references_found_do_not_print_empty(self):
        sample = self.root / "with-ref.md"
        target = self.root / "sub" / "target.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("line one\nline two\n", encoding="utf-8")
        # 带目录分隔符的相对路径才进 checked；裸文件名会被跳过（算 0 处）。
        sample.write_text("见 `sub/target.md:2` 那一行\n", encoding="utf-8")
        code, out = run(["--paths", str(sample), "--root", str(self.root), "--kinds", "line"])
        self.assertEqual(code, 0, out)
        self.assertIn("核了 1 处引用", out)
        self.assertNotIn("EMPTY", out, f"真核到了就不许报 EMPTY，实际输出：{out}")

    def test_relative_reference_still_counts_when_the_file_is_missing(self):
        """指不到东西时走不成立分支（退 1），不会被 EMPTY 遮掉。"""
        sample = self.root / "broken.md"
        sample.write_text("见 `sub/nope.md:1`\n", encoding="utf-8")
        code, out = run(["--paths", str(sample), "--root", str(self.root), "--kinds", "line"])
        self.assertEqual(code, 1, out)
        self.assertNotIn("EMPTY", out, f"有真问题时报的是不成立，不是 EMPTY：{out}")


class RepositoryScopeTest(unittest.TestCase):
    def test_repository_scope_passes(self):
        """本轮结束条件里那条全仓命令，用例里也跑一遍。"""
        code, out = run(["--paths", "docs", "AGENTS.md", "PRD", "tasks", "README.md", "--kinds", "line"])
        self.assertEqual(code, 0, out)
        self.assertIn("裸文件名", last_line(out))
        self.assertIn("绝对路径", last_line(out))


if __name__ == "__main__":
    unittest.main()
