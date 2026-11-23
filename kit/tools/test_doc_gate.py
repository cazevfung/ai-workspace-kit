import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import doc_gate  # noqa: E402


def committed_text(repo: Path, relpath: str):
    """读**提交过**的那份文件（`git show HEAD:<相对路径>`）；取不到时返回 None。

    为什么不直接读工作区那份：`auto_merge` 合完之后会在真实检出上再跑一遍全量
    测试，而收尾包（`tasks/round.md` / `tasks/rounds.md` / `tasks/closing-*.md`）
    天然写在合并之前、可能正写了一半。直接读工作区时，脏收尾包能让合并后的那一场
    测试变红——合并被回滚，原因却与这次改动毫无关系。取提交过的那份，闸门仍钉在
    真实仓库上，但结果不再取决于工作区脏不脏（AW-123）。
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "show", f"HEAD:{relpath}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout if proc.returncode == 0 else None


GOOD = """# 计划：示例

## 依据

- PRD-001 版本 0.4
- 既有事实：69 项测试通过

## 所用 skill

- `plan-gate`：规定了本文件的节结构
- `writing-plans`：改变了任务切分粒度

## 交付物

- 主题：一个可运行的闸门脚本
- 形状：Python 单文件 + unittest
- 不覆盖：不做 CI 集成

## 任务

### 任务 1：写检查器

验证：`python tools/doc_gate.py 本文件`，期望退出码 0

## 不做什么

- 不改 ticket-core 代码

## 未决

无
"""


ROUND_GOOD = """# 本轮

轮次：R-900
状态：进行中

## 本轮目标

1. 让开单前先看这一轮要达成什么

## 结束条件

- `python tools/doc_gate.py --all` 退出码 0

## 不做什么

- 不改 schema
"""


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "plan-test.md"

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")
        return doc_gate.check(self.path)

    def assertMentions(self, problems, fragment):
        self.assertTrue(any(fragment in p for p in problems), problems)

    def test_good_plan_passes(self):
        self.assertEqual([], self.write(GOOD))

    def test_missing_section_fails(self):
        self.assertMentions(self.write(GOOD.replace("## 不做什么", "## 其他")), "缺少必需的二级标题 `## 不做什么`")

    def test_missing_prd_reference_fails(self):
        self.assertMentions(self.write(GOOD.replace("PRD-001 版本 0.4", "看需求文档")), "没有 PRD 编号")

    def test_missing_version_fails(self):
        self.assertMentions(self.write(GOOD.replace("PRD-001 版本 0.4", "PRD-001")), "没有版本号")

    def test_unknown_skill_fails(self):
        self.assertMentions(
            self.write(GOOD.replace("`writing-plans`", "`super-writing-plans`")),
            "引用了不存在的 skill `super-writing-plans`",
        )

    def test_deliverable_needs_subject_and_exclusion(self):
        self.assertMentions(self.write(GOOD.replace("- 不覆盖：不做 CI 集成", "- 做完就行")), "「不覆盖」")

    def test_task_without_evidence_fails(self):
        self.assertMentions(
            self.write(GOOD.replace("验证：`python tools/doc_gate.py 本文件`，期望退出码 0", "小心写")),
            "没有写完成证据",
        )

    def test_bare_placeholder_fails(self):
        self.assertMentions(self.write(GOOD + "\nTODO: 补一句\n"), "裸占位符")

    def test_clarification_without_section_fails(self):
        text = GOOD.replace("## 未决\n\n无\n", "").replace(
            "- 不改 ticket-core 代码",
            "- 不改 ticket-core 代码\n- 有一处 [待澄清：谁来决定] 未定",
        )
        self.assertMentions(self.write(text), "缺少 `## 未决`")

    def test_empty_file_fails(self):
        self.assertMentions(self.write("   \n"), "文件为空")

    def test_named_skill_without_explanation_fails(self):
        text = GOOD.replace(
            "- `writing-plans`：改变了任务切分粒度",
            "- `writing-plans`",
        )
        self.assertMentions(self.write(text), "没有写它改变了什么")

    def test_skill_from_other_project_is_found(self):
        """另一项目的计划引用自己项目的 skill，不应报「不存在」。"""
        other = Path(self.tmp.name) / "另一个项目"
        skill = other / ".agents" / "skills" / "weekly-report"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: weekly-report\ndescription: 周报\n---\n", encoding="utf-8")
        plan = other / "tasks" / "plan-001.md"
        plan.parent.mkdir(parents=True)
        plan.write_text(GOOD.replace("`writing-plans`", "`weekly-report`"), encoding="utf-8")
        self.assertEqual([], doc_gate.check(plan))

    def test_prd_reference_must_point_to_an_existing_prd(self):
        """写个不存在的 PRD 编号，和编一个 skill 名是同一种虚报。"""
        prd = Path(self.tmp.name) / "PRD"
        prd.mkdir()
        (prd / "PRD-001-示例.md").write_text("# 示例 PRD\n", encoding="utf-8")
        plan = Path(self.tmp.name) / "tasks" / "plan-001.md"
        plan.parent.mkdir()
        plan.write_text(GOOD.replace("PRD-001", "PRD-999"), encoding="utf-8")
        self.assertMentions(doc_gate.check(plan), "PRD-999")
        plan.write_text(GOOD, encoding="utf-8")
        self.assertEqual([], doc_gate.check(plan))

    def test_prd_existence_is_not_checked_without_a_prd_directory(self):
        """没有 PRD 目录的项目（比如只引用外部需求）不报这个错，避免误伤。"""
        self.assertEqual([], self.write(GOOD.replace("PRD-001", "PRD-999")))

    def evidence_plan(self, block):
        """把任务块换成给定正文的合规计划，用来单独试「完成证据能不能判」。"""
        text = GOOD.replace("验证：`python tools/doc_gate.py 本文件`，期望退出码 0", block)
        self.path.write_text(text, encoding="utf-8")
        return self.path

    def test_prose_evidence_is_rejected_evidence(self):
        """散文证据（「完成后我会检查」）不可判定，必须非零；只有命令、没写期望输出也一样。"""
        problems = doc_gate.check(self.evidence_plan("验证：完成后我会检查文档准确"))
        self.assertMentions(problems, "完成证据不可判定")
        self.assertMentions(problems, "任务「任务 1：写检查器」")
        bare_command = doc_gate.check(self.evidence_plan("验证：`python tools/run_tests.py`"))
        self.assertMentions(bare_command, "完成证据不可判定")

    def test_command_with_expected_output_passes_evidence(self):
        """判据 A：反引号命令 + 期望输出，算可判定。"""
        self.assertEqual(
            [], doc_gate.check(self.evidence_plan("验证：`grep -c \"^tasks/\" tools/evidence_baseline.txt` → 期望 14"))
        )

    def test_judgement_class_with_counterexample_passes_evidence(self):
        """判据 B：判断类 + 反例，算可判定。"""
        self.assertEqual(
            [],
            doc_gate.check(self.evidence_plan("判断类：口径是否一致；反例：两处写了不同口径")),
        )

    def test_judgement_class_without_counterexample_fails_evidence(self):
        """标了 判断类 却写不出反例，说明这条验收条件本身就不可判。"""
        problems = doc_gate.check(self.evidence_plan("判断类：口径是否一致"))
        self.assertMentions(problems, "完成证据不可判定")

    def test_baseline_skips_only_the_evidence_check_evidence(self):
        """基线只豁免新判据：六节与 skill 的既有检查照跑，且打印跳过行。"""
        text = GOOD.replace(
            "验证：`python tools/doc_gate.py 本文件`，期望退出码 0",
            "验证：完成后我会检查文档准确",
        ).replace("- 不覆盖：不做 CI 集成", "- 做完就行")
        baseline_plan = Path(self.tmp.name) / "plan-baseline.md"
        baseline_plan.write_text(text, encoding="utf-8")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            problems = doc_gate.check(baseline_plan, baseline={"plan-baseline.md": "tasks/plan-baseline.md"})
        self.assertMentions(problems, "「不覆盖」")
        self.assertFalse([p for p in problems if "完成证据不可判定" in p], problems)
        self.assertIn("基线跳过 plan-baseline.md", buffer.getvalue())

    def test_real_plan_file_passes(self):
        plan = Path(__file__).resolve().parent.parent / "tasks" / "plan-002-planning-gate.md"
        if not plan.is_file():
            self.skipTest("计划文件尚未创建")
        self.assertEqual([], doc_gate.check(plan))


class RoundTest(unittest.TestCase):
    """round 文件（本轮目标 / 结束条件 / 不做什么）走它自己的检查。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "round.md"

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")
        return doc_gate.check_round(self.path)

    def assertMentions(self, problems, fragment):
        self.assertTrue(any(fragment in p for p in problems), problems)

    def test_good_round_file_passes(self):
        self.assertEqual([], self.write(ROUND_GOOD))

    def test_round_file_without_round_number_fails(self):
        """没有编号的一轮，在 tasks/rounds.md 里对不上账。"""
        without = "".join(
            line for line in ROUND_GOOD.splitlines(keepends=True) if not line.startswith("轮次：")
        )
        self.assertMentions(self.write(without), "轮次：")

    def test_round_status_must_be_one_of_four(self):
        """`状态：完成了`判不出下一步该干什么，四档之外一律非零。"""
        problems = self.write(ROUND_GOOD.replace("状态：进行中", "状态：完成了"))
        self.assertMentions(problems, "状态")
        self.assertMentions(problems, "只收四档")

    def test_round_file_missing_section_fails(self):
        self.assertMentions(
            self.write(ROUND_GOOD.replace("## 结束条件", "## 别的")),
            "缺少必需的二级标题 `## 结束条件`",
        )

    def test_round_end_condition_needs_a_command(self):
        text = ROUND_GOOD.replace("- `python tools/doc_gate.py --all` 退出码 0", "- 文档精确即可")
        self.assertMentions(self.write(text), "可判定动作")

    def test_round_exclusion_must_not_be_empty(self):
        text = ROUND_GOOD.replace("\n- 不改 schema\n", "\n")
        self.assertMentions(self.write(text), "`## 不做什么` 不能为空")

    def test_round_file_without_required_sections_at_all_fails(self):
        problems = self.write(GOOD)
        self.assertMentions(problems, "缺少必需的二级标题 `## 本轮目标`")

    def test_round_file_routed_by_name(self):
        """按文件名分流：round*.md 走 round 检查，计划文件仍走 check()。"""
        self.assertTrue(doc_gate.is_round_file(Path("tasks/round.md")))
        self.assertTrue(doc_gate.is_round_file(Path("tasks/round-001.md")))
        self.assertFalse(doc_gate.is_round_file(Path("tasks/plan-017-x.md")))

    def test_rounds_ledger_is_not_a_round_file(self):
        """`tasks/rounds.md` 是历史账本（一张表），不是「本轮目标」文件。

        它按名字本来会撞上 `round*.md`：不排除掉的话 `--all` 会拿三节判据
        去查一张表，每次收尾都多三条假红。
        """
        self.assertFalse(doc_gate.is_round_file(Path("tasks/rounds.md")))

    def test_all_ignores_the_rounds_ledger(self):
        root = Path(self.tmp.name)
        (root / "tasks").mkdir()
        (root / "tasks" / "plan-001.md").write_text(GOOD, encoding="utf-8")
        (root / "tasks" / "round.md").write_text(ROUND_GOOD, encoding="utf-8")
        (root / "tasks" / "rounds.md").write_text("# 账本\n\n| 轮次 |\n| --- |\n| R-001 |\n", encoding="utf-8")
        targets, problems = doc_gate.scan(root)
        self.assertEqual([], problems)
        self.assertNotIn(root / "tasks" / "rounds.md", targets)

    def write_target(self):
        self.path.write_text(ROUND_GOOD, encoding="utf-8")
        return self.path

    def test_main_accepts_a_round_file(self):
        self.assertEqual(0, doc_gate.main([str(self.write_target())]))

    def test_main_rejects_a_broken_round_file(self):
        self.path.write_text(ROUND_GOOD.replace("## 结束条件", "## 别的"), encoding="utf-8")
        self.assertEqual(1, doc_gate.main([str(self.path)]))

    def test_all_without_round_file_fails(self):
        root = Path(self.tmp.name)
        (root / "tasks").mkdir()
        (root / "tasks" / "plan-001.md").write_text(GOOD, encoding="utf-8")
        targets, problems = doc_gate.scan(root)
        self.assertIn(root / "tasks" / "plan-001.md", targets)
        self.assertMentions(problems, "tasks/round.md")

    def test_all_with_round_file_finds_it(self):
        root = Path(self.tmp.name)
        (root / "tasks").mkdir()
        (root / "tasks" / "plan-001.md").write_text(GOOD, encoding="utf-8")
        (root / "tasks" / "round.md").write_text(ROUND_GOOD, encoding="utf-8")
        targets, problems = doc_gate.scan(root)
        self.assertEqual([], problems)
        self.assertIn(root / "tasks" / "round.md", targets)

    def test_real_round_file_passes(self):
        """仓库根那份 round 文件（**提交过的**那一版）要过闸门。

        原来直接读工作区那份。改成读 `HEAD` 里那份，是因为 `auto_merge` 合完之后会
        在真实检出上再跑一遍全量测试，而收尾包天然写在合并之前、可能正写了一半：
        工作区的内容不该决定这一场测试的结果（AW-123）。闸门仍然钉在真实仓库上，
        只是证据换成已提交的那一版。`--all` 那份检查也从这里拿掉——它同样会读
        工作区的 round 文件，而 `--all` 的两组目标（plan 文件 + round 文件）已由
        下面两个用例各自覆盖。
        """
        repo = Path(__file__).resolve().parent.parent
        text = committed_text(repo, "tasks/round.md")
        if text is None:
            self.skipTest("取不到 tasks/round.md 的提交内容（不是 git 仓库或缺这一版）")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "round.md"
            target.write_text(text, encoding="utf-8")
            self.assertEqual([], doc_gate.check_round(target))
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = doc_gate.main([str(target)])
            self.assertEqual(0, code, buffer.getvalue())
            self.assertIn("闸门通过", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
