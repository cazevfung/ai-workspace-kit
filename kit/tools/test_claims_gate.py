"""claims_gate 的用例：每条判据都能红一把，闸门红不了就是它没有牙。

形状照 `tools/test_close_gate.py` 写：每条判据各有一个具名用例，验收时在
`-v` 输出里能逐条看到「它曾经红过」。四类断言各自一正一反：

    数量类（count / sum / ratio）  正例 3 条，反例 3 条
    摘要类（sha256 / size）        正例 2 条，反例 2 条
    位置类（anchor）               正例 1 条，反例 2 条
    清单类（list）                 正例 1 条，反例 1 条

临时目录里的期望值一律由用例自己算（`hashlib` / `len` / 数出来的行数），不手抄。
手抄的期望值等于把错数换个文件放，用例先就失去了判据的意义。
"""

import contextlib
import hashlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import claims_gate

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "claims"

# 四个反例 fixture 的期望：文件 + 它必须指到的位置前缀。
BAD_FIXTURES = {
    "bad-count.json": "report.md:20",
    "bad-hash.json": "report.md:1",
    "bad-anchor.json": "report.md:999",
    "bad-list.json": "report.md:20",
}

SAMPLE_DOC = """# 样本

## 一、分组

- 组甲：3 份
- 组乙：5 份

## 二、清单

- [x] 甲
- [ ] 乙
"""


def run(argv):
    """跑一次 main，返回 (退出码, stdout, stderr)。argparse 的 SystemExit 也收成退出码。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = claims_gate.main(list(argv))
        except SystemExit as exc:  # --help 走的是这条路
            code = exc.code if isinstance(exc.code, int) else 0
    return code, out.getvalue(), err.getvalue()


class FixtureTest(unittest.TestCase):
    """跑仓库里那五份 fixture：正例绿、四份反例各自红并点名位置。"""

    def test_help_exits_zero_and_names_the_four_classes(self):
        code, out, _err = run(["--help"])
        self.assertEqual(0, code, out)
        for name in ("count", "sum", "ratio", "sha256", "size", "anchor", "list"):
            self.assertIn(name, out, f"--help 里看不到 {name} 这种断言")
        for group in ("数量类", "摘要类", "位置类", "清单类"):
            self.assertIn(group, out, f"--help 里看不到「{group}」这一类的名字")

    def test_good_fixture_passes(self):
        code, out, _err = run(["--claims", str(FIXTURES / "good.json")])
        self.assertEqual(0, code, out)
        self.assertIn("7 / 7", out)

    def test_four_bad_fixtures_are_red_and_point_at_a_md_line(self):
        for name, where in BAD_FIXTURES.items():
            with self.subTest(fixture=name):
                code, out, _err = run(["--claims", str(FIXTURES / name)])
                self.assertEqual(1, code, out)
                self.assertIn(where, out)
                self.assertRegex(out, r"\.md:\d+")

    def test_emit_prints_observed_values_for_every_claim(self):
        code, out, _err = run(["--claims", str(FIXTURES / "good.json"), "--emit"])
        self.assertEqual(0, code, out)
        for claim_id in ("C1", "C2", "C3", "C5", "C7"):
            self.assertRegex(out, rf"(?m)^{claim_id}\t")


class SandboxTest(unittest.TestCase):
    """临时目录里一条一条造：每类断言各一正一反，期望值都由用例自己算出来。"""

    def project(self, files, claims):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, content in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8")
        # 用例起名的预期：claims.json 就放在这份项目根上，file 相对它解析。
        path = root / "claims.json"
        path.write_text(
            json.dumps({"version": 1, "claims": claims}, ensure_ascii=False), encoding="utf-8"
        )
        return path

    def gate(self, files, claims):
        return run(["--claims", str(self.project(files, claims))])

    def claim(self, **over):
        base = {"id": "T1", "kind": "count", "file": "doc.md", "source": "用例现算"}
        base.update(over)
        return base

    # ---- 数量类：count -------------------------------------------------

    def test_count_matches(self):
        expected = len(re.findall(r"(?m)^- 组", SAMPLE_DOC))
        code, out, _err = self.gate(
            {"doc.md": SAMPLE_DOC},
            [self.claim(kind="count", pattern="^- 组", regex=True, expect=expected)],
        )
        self.assertEqual(0, code, out)

    def test_count_mismatch_is_red(self):
        actual = len(re.findall(r"(?m)^- 组", SAMPLE_DOC))
        code, out, _err = self.gate(
            {"doc.md": SAMPLE_DOC},
            [self.claim(kind="count", line=3, pattern="^- 组", regex=True, expect=actual + 1)],
        )
        self.assertEqual(1, code, out)
        self.assertIn("doc.md:3", out)
        self.assertIn(f"实测 {actual}", out)

    # ---- 数量类：sum ---------------------------------------------------

    def test_sum_matches(self):
        numbers = [int(n) for n in re.findall(r"(?m)^- 组.：(\d+) 份$", SAMPLE_DOC)]
        self.assertTrue(numbers)  # 样本没取到数的话，这条用例什么也没证明
        code, out, _err = self.gate(
            {"doc.md": SAMPLE_DOC},
            [self.claim(kind="sum", pattern=r"^- 组.：(\d+) 份$", expect=sum(numbers))],
        )
        self.assertEqual(0, code, out)

    def test_sum_mismatch_is_red(self):
        numbers = [int(n) for n in re.findall(r"(?m)^- 组.：(\d+) 份$", SAMPLE_DOC)]
        code, out, _err = self.gate(
            {"doc.md": SAMPLE_DOC},
            [self.claim(kind="sum", line=2, pattern=r"^- 组.：(\d+) 份$", expect=sum(numbers) + 2)],
        )
        self.assertEqual(1, code, out)
        self.assertIn("doc.md:2", out)
        self.assertIn(f"实测 {sum(numbers)}", out)

    # ---- 数量类：ratio -------------------------------------------------

    def test_ratio_matches(self):
        done = len(re.findall(r"(?m)^- \[x\] ", SAMPLE_DOC))
        total = len(re.findall(r"(?m)^- \[.\] ", SAMPLE_DOC))
        code, out, _err = self.gate(
            {"doc.md": SAMPLE_DOC},
            [
                self.claim(
                    kind="ratio",
                    numerator=r"^- \[x\] ",
                    denominator=r"^- \[.\] ",
                    digits=2,
                    expect=round(done / total, 2),
                )
            ],
        )
        self.assertEqual(0, code, out)

    def test_ratio_mismatch_is_red(self):
        done = len(re.findall(r"(?m)^- \[x\] ", SAMPLE_DOC))
        total = len(re.findall(r"(?m)^- \[.\] ", SAMPLE_DOC))
        code, out, _err = self.gate(
            {"doc.md": SAMPLE_DOC},
            [
                self.claim(
                    kind="ratio",
                    line=4,
                    numerator=r"^- \[x\] ",
                    denominator=r"^- \[.\] ",
                    digits=2,
                    expect=round(done / total, 2) + 0.1,
                )
            ],
        )
        self.assertEqual(1, code, out)
        self.assertIn("doc.md:4", out)

    def test_ratio_with_empty_denominator_is_red(self):
        code, out, _err = self.gate(
            {"doc.md": SAMPLE_DOC},
            [
                self.claim(
                    kind="ratio",
                    numerator=r"^- \[x\] ",
                    denominator=r"这个名字在样本里一次也不出现",
                    expect=0.5,
                )
            ],
        )
        self.assertEqual(1, code, out)
        self.assertIn("分母", out)

    # ---- 摘要类：sha256 / size -----------------------------------------

    def test_sha256_matches(self):
        payload = b"\x89PNG\r\n\x1a\n\x00payload"
        code, out, _err = self.gate(
            {"a.bin": payload},
            [self.claim(kind="sha256", file="a.bin", expect=hashlib.sha256(payload).hexdigest())],
        )
        self.assertEqual(0, code, out)

    def test_sha256_mismatch_is_red(self):
        payload = b"\x89PNG\r\n\x1a\n\x00payload"
        code, out, _err = self.gate(
            {"a.md": payload},
            [self.claim(kind="sha256", file="a.md", expect=hashlib.sha256(b"").hexdigest())],
        )
        self.assertEqual(1, code, out)
        self.assertIn("a.md:1", out)

    def test_size_matches(self):
        payload = "一二三\n".encode("utf-8")
        code, out, _err = self.gate(
            {"a.md": payload}, [self.claim(kind="size", file="a.md", expect=len(payload))]
        )
        self.assertEqual(0, code, out)

    def test_size_mismatch_is_red(self):
        payload = "一二三\n".encode("utf-8")
        code, out, _err = self.gate(
            {"a.md": payload}, [self.claim(kind="size", file="a.md", expect=len(payload) + 1)]
        )
        self.assertEqual(1, code, out)
        self.assertIn("a.md:1", out)

    # ---- 位置类：anchor ------------------------------------------------

    def test_anchor_matches(self):
        code, out, _err = self.gate(
            {"doc.md": SAMPLE_DOC},
            [self.claim(kind="anchor", line=6, contains="组乙：5 份")],
        )
        self.assertEqual(0, code, out)

    def test_anchor_on_other_text_is_red(self):
        code, out, _err = self.gate(
            {"doc.md": SAMPLE_DOC},
            [self.claim(kind="anchor", line=5, contains="组丙：9 份")],
        )
        self.assertEqual(1, code, out)
        self.assertIn("doc.md:5", out)

    def test_anchor_beyond_end_of_file_is_red(self):
        code, out, _err = self.gate(
            {"doc.md": SAMPLE_DOC},
            [self.claim(kind="anchor", line=999, contains="随便什么")],
        )
        self.assertEqual(1, code, out)
        self.assertIn("doc.md:999", out)
        self.assertIn("只有", out)

    # ---- 清单类：list --------------------------------------------------

    def test_list_matches(self):
        code, out, _err = self.gate(
            {"doc.md": SAMPLE_DOC},
            [self.claim(kind="list", section="二、清单", expect=2)],
        )
        self.assertEqual(0, code, out)

    def test_list_mismatch_is_red(self):
        code, out, _err = self.gate(
            {"doc.md": SAMPLE_DOC},
            [self.claim(kind="list", section="二、清单", expect=3)],
        )
        self.assertEqual(1, code, out)
        self.assertIn("doc.md:1", out)
        self.assertIn("实测 2", out)

    def test_list_section_not_found_is_red(self):
        code, out, _err = self.gate(
            {"doc.md": SAMPLE_DOC},
            [self.claim(kind="list", section="九、没有这一节", expect=0)],
        )
        self.assertEqual(1, code, out)
        self.assertIn("没有找到", out)

    # ---- claims 文件本身的形状 -----------------------------------------

    def test_missing_source_is_a_usage_error(self):
        code, out, err = self.gate(
            {"doc.md": SAMPLE_DOC},
            [{"id": "T1", "kind": "count", "file": "doc.md", "pattern": "x", "expect": 0}],
        )
        self.assertEqual(2, code, out + err)
        self.assertIn("source", out + err)

    def test_unknown_kind_is_a_usage_error(self):
        code, out, err = self.gate(
            {"doc.md": SAMPLE_DOC}, [self.claim(kind="vibes", expect=1)]
        )
        self.assertEqual(2, code, out + err)
        self.assertIn("vibes", out + err)

    def test_bad_hash_length_is_a_usage_error(self):
        code, out, err = self.gate(
            {"doc.md": SAMPLE_DOC}, [self.claim(kind="sha256", file="doc.md", expect="abc")]
        )
        self.assertEqual(2, code, out + err)
        self.assertIn("64", out + err)

    def test_missing_target_file_is_red_not_a_crash(self):
        code, out, _err = self.gate(
            {"doc.md": SAMPLE_DOC},
            [self.claim(kind="count", file="nope.md", pattern="x", expect=0)],
        )
        self.assertEqual(1, code, out)
        self.assertIn("nope.md", out)
        self.assertIn("不存在", out)

    def test_broken_json_is_a_usage_error(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "claims.json"
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        code, out, err = run(["--claims", str(path)])
        self.assertEqual(2, code, out + err)

    def test_no_arguments_is_a_usage_error(self):
        code, out, _err = run([])
        self.assertEqual(2, code, out)


BARE_DOC = """# 样本

## 一、合计

合计 37。
"""

UNIT_DOC = """# 样本

## 一、合计

合计 37 处没有 claim 核过。
"""

REF_DOC = """# 样本

## 一、引用

这一段在 claim_gate.py:131 里也出现过。
"""

FENCED_DOC = """# 样本

正文没有数。

```python
value = 37
```
"""

ORDINAL_DOC = """# 样本

## 一步一步

1. 第一步没有量。
2. 第二步也没有量。

### 8.1 子标题
"""


class CoverageTest(unittest.TestCase):
    """`--coverage`：只列出没有被任何 claim 覆盖的活数位置，不判对错、不非零退出。"""

    def project(self, files, claims):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, content in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        path = root / "claims.json"
        path.write_text(
            json.dumps({"version": 1, "claims": claims}, ensure_ascii=False), encoding="utf-8"
        )
        return root, path

    def coverage(self, files, claims, paths=("doc.md",)):
        root, claims_path = self.project(files, claims)
        argv = ["--coverage", "--claims", str(claims_path), "--paths"]
        argv.extend(str(root / p) for p in paths)
        return run(argv)

    def claim(self, **over):
        base = {"id": "T1", "kind": "count", "file": "doc.md", "source": "用例现算"}
        base.update(over)
        return base

    def unrelated(self):
        """一条只碰别的文件的 claim，保证 claims 非空而 doc.md 一点没被覆盖。"""
        return self.claim(
            id="U1", file="other.md", line=1, pattern="^x", expect=0
        )

    def test_new_bare_number_is_listed_with_position_and_raw_line(self):
        code, out, _err = self.coverage({"doc.md": BARE_DOC}, [self.unrelated()])
        self.assertEqual(0, code, out)
        self.assertIn("doc.md:5", out)
        self.assertIn("合计 37。", out)
        self.assertIn("裸数", out)

    def test_number_with_unit_is_listed_as_its_own_kind(self):
        code, out, _err = self.coverage({"doc.md": UNIT_DOC}, [self.unrelated()])
        self.assertEqual(0, code, out)
        self.assertIn("doc.md:5", out)
        self.assertIn("数+量词", out)

    def test_file_colon_line_reference_is_listed(self):
        code, out, _err = self.coverage({"doc.md": REF_DOC}, [self.unrelated()])
        self.assertEqual(0, code, out)
        self.assertIn("doc.md:5", out)
        self.assertIn("文件:行号", out)

    def test_position_a_claim_names_does_not_appear(self):
        code, out, _err = self.coverage(
            {"doc.md": UNIT_DOC}, [self.claim(kind="anchor", line=5, contains="合计 37 处")]
        )
        self.assertEqual(0, code, out)
        self.assertIn("未被覆盖 0 处", out)
        self.assertNotIn("doc.md:5", out)

    def test_whole_file_claim_covers_every_line(self):
        code, out, _err = self.coverage(
            {"doc.md": BARE_DOC},
            [self.claim(kind="sha256", file="doc.md", expect=hashlib.sha256(BARE_DOC.encode()).hexdigest())],
        )
        self.assertEqual(0, code, out)
        self.assertIn("未被覆盖 0 处", out)

    def test_pattern_claim_covers_the_lines_it_matches(self):
        code, out, _err = self.coverage(
            {"doc.md": UNIT_DOC},
            [self.claim(kind="sum", pattern=r"合计 (\d+) 处", expect=37)],
        )
        self.assertEqual(0, code, out)
        self.assertIn("未被覆盖 0 处", out)

    def test_zero_is_still_reported_as_a_line(self):
        code, out, _err = self.coverage(
            {"doc.md": "# 样本\n\n正文没有数字。\n"}, [self.unrelated()]
        )
        self.assertEqual(0, code, out)
        self.assertRegex(out, r"(?m)^未被覆盖 0 处")

    def test_uncovered_positions_still_exit_zero(self):
        code, out, _err = self.coverage({"doc.md": BARE_DOC}, [self.unrelated()])
        self.assertEqual(0, code, out)
        self.assertRegex(out, r"(?m)^未被覆盖 [1-9]\d* 处")

    def test_examples_inside_a_code_fence_are_not_counted(self):
        code, out, _err = self.coverage({"doc.md": FENCED_DOC}, [self.unrelated()])
        self.assertEqual(0, code, out)
        self.assertIn("未被覆盖 0 处", out)

    def test_line_leading_ordinals_are_not_live_numbers(self):
        code, out, _err = self.coverage({"doc.md": ORDINAL_DOC}, [self.unrelated()])
        self.assertEqual(0, code, out)
        self.assertIn("未被覆盖 0 处", out)

    def test_a_number_at_line_start_with_a_unit_is_still_listed(self):
        code, out, _err = self.coverage(
            {"doc.md": "37 处没有 claim 核过。\n"}, [self.unrelated()]
        )
        self.assertEqual(0, code, out)
        self.assertIn("doc.md:1", out)
        self.assertIn("数+量词", out)

    def test_coverage_without_paths_is_a_usage_error(self):
        _root, claims_path = self.project({"doc.md": BARE_DOC}, [self.unrelated()])
        code, out, err = run(["--coverage", "--claims", str(claims_path)])
        self.assertEqual(2, code, out + err)

    def test_coverage_with_emit_is_a_usage_error(self):
        _root, claims_path = self.project({"doc.md": BARE_DOC}, [self.unrelated()])
        code, out, err = run(["--coverage", "--emit", "--claims", str(claims_path), "--paths", "doc.md"])
        self.assertEqual(2, code, out + err)

    def test_repo_fixture_is_fully_covered(self):
        code, out, _err = run(
            [
                "--coverage",
                "--claims",
                str(FIXTURES / "good.json"),
                "--paths",
                str(FIXTURES / "report.md"),
            ]
        )
        self.assertEqual(0, code, out)
        self.assertIn("未被覆盖 0 处", out)

    def test_coverage_command_from_the_ticket_points_at_docs(self):
        code, out, _err = run(
            ["--coverage", "--claims", str(FIXTURES / "good.json"), "--paths", str(ROOT / "docs")]
        )
        self.assertEqual(0, code, out)
        self.assertRegex(out, r"(?m)^未被覆盖 \d+ 处")


if __name__ == "__main__":
    unittest.main()
    unittest.main()
