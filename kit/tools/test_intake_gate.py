import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import intake_gate


HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "tests" / "fixtures" / "intake"
GOOD_PACK = FIXTURES / "1205-配乐-交接包.md"
OLD_FORMAT = FIXTURES / "1205-配乐-旧格式.md"

QUOTE_KEEP = "我觉得做的还是不够好"
QUOTE_SKILL = "魔改别人的skills"


def load_good():
    return GOOD_PACK.read_text(encoding="utf-8")


def drop_section(text, number):
    """整节删掉（标题连同正文），用于「六节各删一节」的矩阵。"""
    lines = text.splitlines(keepends=True)
    out = []
    skipping = False
    for line in lines:
        if line.startswith("## "):
            skipping = line.startswith(f"## {number}. ")
        if not skipping:
            out.append(line)
    return "".join(out)


class PackCheckTest(unittest.TestCase):
    """按形状判定的五条判据（PRD-009 §4.8）：每条都要能被一个反例推翻。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "pack.md"

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")
        return intake_gate.check(self.path)

    def assertMentions(self, problems, fragment):
        self.assertTrue(any(fragment in p for p in problems), problems)

    def test_good_pack_passes(self):
        self.assertEqual([], intake_gate.check(GOOD_PACK))

    # 六节各删一节：不是一个循环断言，是六个具名用例——验收时要能在 -v 输出里逐个看到。
    def test_missing_section_1_fails(self):
        self.assertMentions(self.write(drop_section(load_good(), 1)), "缺少第 1 节")

    def test_missing_section_2_fails(self):
        self.assertMentions(self.write(drop_section(load_good(), 2)), "缺少第 2 节")

    def test_missing_section_3_fails(self):
        self.assertMentions(self.write(drop_section(load_good(), 3)), "缺少第 3 节")

    def test_missing_section_4_fails(self):
        self.assertMentions(self.write(drop_section(load_good(), 4)), "缺少第 4 节")

    def test_missing_section_5_fails(self):
        self.assertMentions(self.write(drop_section(load_good(), 5)), "缺少第 5 节")

    def test_missing_section_6_fails(self):
        self.assertMentions(self.write(drop_section(load_good(), 6)), "缺少第 6 节")

    def test_section_headings_are_not_paraphrased(self):
        """标题文字改一个字就是缺节：闸门按固定标题匹配，不做近义猜测。"""
        self.assertMentions(
            self.write(load_good().replace("## 4. 硬约束", "## 4. 硬性约束")),
            "缺少第 4 节",
        )

    def test_section_order_must_not_change(self):
        text = load_good()
        head, rest = text.split("## 3. 做过什么、为什么不行", 1)
        third, tail = rest.split("## 4. 硬约束", 1)
        swapped = head + "## 4. 硬约束" + tail.split("## 5. 已确认的事实", 1)[0] + "## 3. 做过什么、为什么不行" + third
        swapped += "## 5. 已确认的事实" + tail.split("## 5. 已确认的事实", 1)[1]
        self.assertMentions(self.write(swapped), "顺序")

    def test_section_1_without_quote_block_fails(self):
        """把原话改写成普通段落（不是引用块）——「逐字」这件事就没人能核对了。"""
        head, rest = load_good().split("## 1. 用户原话", 1)
        first, tail = rest.split("## 2. 要交付什么", 1)
        unquoted = "\n".join(
            line.lstrip()[2:] if line.lstrip().startswith("> ") else line
            for line in first.splitlines()
        )
        self.assertMentions(
            self.write(head + "## 1. 用户原话\n" + unquoted + "\n## 2. 要交付什么" + tail),
            "第 1 节",
        )

    def test_section_3_placeholder_fails(self):
        head, rest = load_good().split("## 3. 做过什么、为什么不行", 1)
        _, tail = rest.split("## 4. 硬约束", 1)
        self.assertMentions(
            self.write(head + "## 3. 做过什么、为什么不行\n\n待补充\n\n## 4. 硬约束" + tail),
            "第 3 节",
        )

    def test_section_3_todo_placeholder_fails(self):
        head, rest = load_good().split("## 3. 做过什么、为什么不行", 1)
        _, tail = rest.split("## 4. 硬约束", 1)
        self.assertMentions(
            self.write(head + "## 3. 做过什么、为什么不行\n\nTODO\n\n## 4. 硬约束" + tail),
            "第 3 节",
        )

    def test_section_5_without_absolute_path_fails(self):
        head, rest = load_good().split("## 5. 已确认的事实", 1)
        _, tail = rest.split("## 6. 未决问题", 1)
        self.assertMentions(
            self.write(head + "## 5. 已确认的事实\n\n看过 srt 与 suno 文件夹，都还在。\n\n## 6. 未决问题" + tail),
            "绝对路径",
        )

    def test_old_format_message_fails(self):
        """这次事故的真实输入：三行消息没有六节，必须非零。"""
        self.assertMentions(intake_gate.check(OLD_FORMAT), "缺少第 1 节")

    def test_missing_file_fails(self):
        self.assertMentions(intake_gate.check(Path(self.tmp.name) / "nope.md"), "不存在")

    def test_non_utf8_file_fails(self):
        path = Path(self.tmp.name) / "gbk.md"
        path.write_bytes("## 1. 用户原话\n".encode("gbk") + b"\xff\xfe")
        self.assertMentions(intake_gate.check(path), "UTF-8")


class CliTest(unittest.TestCase):
    """退出码是闸门的对外契约：0 通过、1 有问题、2 用法错误。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = intake_gate.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_cli_passes_good_pack(self):
        code, out, _ = self.run_main([str(GOOD_PACK)])
        self.assertEqual(0, code, out)

    def test_cli_rejects_old_format(self):
        code, _out, err = self.run_main([str(OLD_FORMAT)])
        self.assertEqual(1, code)
        self.assertIn("第 1 节", err)

    def test_cli_usage_error_without_argument(self):
        code, _out, err = self.run_main([])
        self.assertEqual(2, code)
        self.assertIn("用法", err)

    def test_extract_prints_only_section_1(self):
        code, out, _ = self.run_main(["--extract", str(GOOD_PACK)])
        self.assertEqual(0, code)
        self.assertIn(QUOTE_KEEP, out)
        self.assertIn(QUOTE_SKILL, out)
        self.assertNotIn("## 2.", out)
        self.assertNotIn("## 6.", out)
        self.assertNotIn("不重做视频工程", out)

    def test_extract_flag_after_path_also_works(self):
        code, out, _ = self.run_main([str(GOOD_PACK), "--extract"])
        self.assertEqual(0, code)
        self.assertIn(QUOTE_KEEP, out)

    def test_extract_on_broken_pack_fails_instead_of_printing_garbage(self):
        path = Path(self.tmp.name) / "half.md"
        path.write_text("## 1. 用户原话\n\n> 一句原话\n", encoding="utf-8")
        code, out, err = self.run_main(["--extract", str(path)])
        self.assertEqual(1, code)
        self.assertEqual("", out.strip())
        self.assertIn("缺少第 2 节", err)


if __name__ == "__main__":
    unittest.main()
