"""close_gate 的用例：每条判据一条反例，闸门红不了就是它没有牙。

形状照 `tools/test_intake_gate.py` 写：每一条判据各有一个具名用例，
验收时能在 `-v` 输出里逐条看到「它曾经红过」。
"""

import contextlib
import io
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import close_gate
import work_gate

ROOT = Path(__file__).resolve().parent.parent


def committed_text(repo: Path, relpath: str):
    """读**提交过**的那份文件（`git show HEAD:<相对路径>`）；取不到时返回 None。

    为什么不直接读工作区那份：`auto_merge` 合完之后会在真实检出上再跑一遍全量
    测试，而收尾包（`tasks/round.md` / `tasks/rounds.md` / `tasks/closing-*.md`）
    天然写在合并之前、可能正写了一半。直接读工作区时，脏收尾包能让合并后的那一场
    测试变红——合并被回滚，原因却与这次改动毫无关系（AW-123）。
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "show", f"HEAD:{relpath}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout if proc.returncode == 0 else None


def committed_closing_packages(repo: Path) -> list[str]:
    """`HEAD` 里已经提交过的 `tasks/closing-*.md`，相对仓库根的名称（排序后）。"""
    proc = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "--name-only", "HEAD", "tasks/"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        return []
    names = []
    for line in proc.stdout.splitlines():
        name = Path(line.strip()).name
        if name.startswith("closing-") and name.endswith(".md"):
            names.append(line.strip())
    return sorted(names)

GOOD_ROUND = """# 本轮：示例

轮次：R-900
状态：已收尾

## 本轮目标

1. 让「这一轮做完了没有」有一个能失败的判据

## 结束条件

- `python tools/run_tests.py` 全绿

## 不做什么

- 不改 schema
"""

GOOD_PACK = """# 收尾包：R-900

## 1. 本轮原话目标

> 把这一轮做完，别只说做完了。

## 2. 已完成及证据

- `python tools/run_tests.py` 336 项全绿。

## 3. 未做到或偏离

无，因为每条目标都有对应证据，见第 2 节。

## 4. 决定不做及原因

- 不做定时扫描器：本轮不覆盖这一层。

## 5. 欠 AI 的下一步

- 下一轮把规范条目补进 PRD-001。

## 6. 欠人的一条可执行动作

- 打开 `tasks/round.md`，把 R-004 的目标写成三条。

## 7. 结论

已收尾。
"""


def git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check
    )


def make_git_repo(root):
    """一个干净的空仓库：三条事实里的第一条要有真的 `git status` 可跑。"""
    git(root, "init", "-q")
    git(root, "config", "user.email", "close-gate@example.invalid")
    git(root, "config", "user.name", "close gate")
    (root / "README.md").write_text("示例\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-q", "-m", "初始提交")


def make_db(path, rows=()):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, project_id TEXT, state TEXT)")
    conn.execute("DELETE FROM tickets")
    conn.executemany("INSERT INTO tickets (id, project_id, state) VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()


class PackageTest(unittest.TestCase):
    """七节结构：缺节、顺序、第 3 节、第 7 节三档。每条判据一条反例。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "tasks").mkdir()
        self.round = self.root / "tasks" / "round.md"
        self.pack = self.root / "tasks" / "closing-R-900.md"
        self.round.write_text(GOOD_ROUND, encoding="utf-8")
        self.pack.write_text(GOOD_PACK, encoding="utf-8")

    def write_pack(self, text, path=None):
        (path or self.pack).write_text(text, encoding="utf-8")
        return close_gate.check(self.root)

    def write_round(self, text):
        self.round.write_text(text, encoding="utf-8")
        return close_gate.check(self.root)

    def write_section_5(self, body):
        """只换第 5 节的正文，其余六节保持 GOOD_PACK 原样。"""
        head, rest = GOOD_PACK.split("## 5. 欠 AI 的下一步", 1)
        tail = rest.split("## 6. 欠人的一条可执行动作", 1)[1]
        return self.write_pack(
            head + "## 5. 欠 AI 的下一步\n\n" + body + "\n\n## 6. 欠人的一条可执行动作" + tail
        )

    def assertMentions(self, problems, fragment):
        self.assertTrue(any(fragment in p for p in problems), problems)

    def test_good_package_passes(self):
        self.assertEqual([], close_gate.check(self.root))

    def test_missing_section_3_fails(self):
        problems = self.write_pack(GOOD_PACK.split("## 3. 未做到或偏离", 1)[0] + "## 4." + GOOD_PACK.split("## 4.", 1)[1])
        self.assertMentions(problems, "缺少第 3 节")

    def test_section_3_bare_none_fails(self):
        """只写一个「无」就是没写：看不到「我确认过」。"""
        head, rest = GOOD_PACK.split("## 3. 未做到或偏离", 1)
        tail = rest.split("## 4. 决定不做及原因", 1)[1]
        problems = self.write_pack(head + "## 3. 未做到或偏离\n\n无\n\n## 4. 决定不做及原因" + tail)
        self.assertMentions(problems, "第 3 节")
        self.assertMentions(problems, "无")

    def test_section_3_placeholder_fails(self):
        head, rest = GOOD_PACK.split("## 3. 未做到或偏离", 1)
        tail = rest.split("## 4. 决定不做及原因", 1)[1]
        problems = self.write_pack(head + "## 3. 未做到或偏离\n\n待补充\n\n## 4. 决定不做及原因" + tail)
        self.assertMentions(problems, "第 3 节")

    def test_verdict_free_text_fails(self):
        """「基本做完了」判不出下一步该干什么，所以非零。"""
        problems = self.write_pack(GOOD_PACK.replace("## 7. 结论\n\n已收尾。", "## 7. 结论\n\n基本做完了。"))
        self.assertMentions(problems, "三档")

    def test_section_headings_are_not_paraphrased(self):
        problems = self.write_pack(GOOD_PACK.replace("## 4. 决定不做及原因", "## 4. 不做的原因"))
        self.assertMentions(problems, "缺少第 4 节")

    def test_section_order_must_not_change(self):
        third = GOOD_PACK.split("## 3. 未做到或偏离", 1)[1].split("## 4.", 1)[0]
        fourth = GOOD_PACK.split("## 4. 决定不做及原因", 1)[1].split("## 5.", 1)[0]
        swapped = (
            GOOD_PACK.split("## 3. 未做到或偏离", 1)[0]
            + "## 4. 决定不做及原因" + fourth
            + "## 3. 未做到或偏离" + third
            + "## 5." + GOOD_PACK.split("## 5.", 1)[1]
        )
        self.assertMentions(self.write_pack(swapped), "顺序")

    def test_section_5_item_without_locator_fails(self):
        """「补进第二张单」既没有单号、没有路径、也没有命令：下一个会话无从下手。"""
        problems = self.write_section_5("- 补进第二张单。")
        self.assertMentions(problems, "第 5 节")
        self.assertMentions(problems, "补进第二张单")

    def test_section_5_one_bad_item_among_good_ones_fails(self):
        """判据是「每条」：同一节里有一条没落点，整节就不算过。"""
        problems = self.write_section_5("- 下一轮把 `tools/close_gate.py` 的第 4 节判据补上。\n- 继续优化。")
        self.assertMentions(problems, "第 5 节")
        self.assertMentions(problems, "继续优化")

    def test_section_5_with_ticket_id_passes(self):
        self.assertEqual([], self.write_section_5("- 接手 AW-109，把 parking 表补上。"))

    def test_section_5_with_file_path_passes(self):
        self.assertEqual([], self.write_section_5("- 改 docs/closing.md 的第 5 节示例。"))

    def test_section_5_with_command_passes(self):
        self.assertEqual([], self.write_section_5("- 跑 `python tools/run_tests.py` 复核基线。"))

    def test_section_5_empty_fails(self):
        """一条都不写等于没说这些东西去哪了：要么给落点，要么写进第 4 节。"""
        problems = self.write_section_5("")
        self.assertMentions(problems, "第 5 节")
        self.assertMentions(problems, "第 4 节")

    def test_section_5_continuation_lines_stay_with_their_item(self):
        """续行并进同一条：长句换行不该被当成第二条去判。"""
        problems = self.write_section_5(
            "- 接手 AW-109\n  把 parking 表补上，判据见 `PRD/PRD-014-计划队列与依赖.md`。"
        )
        self.assertEqual([], problems)

    def test_missing_package_fails(self):
        self.pack.unlink()
        self.assertMentions(close_gate.check(self.root), "不存在")

    def test_non_utf8_package_fails(self):
        self.pack.write_bytes("## 1. 本轮原话目标\n".encode("gbk") + b"\xff\xfe")
        self.assertMentions(close_gate.check(self.root), "UTF-8")

    def test_round_status_in_progress_says_no_package(self):
        """本轮状态还是「进行中」时，那句话要原样出现——C6 就是这一条。"""
        problems = self.write_round(GOOD_ROUND.replace("状态：已收尾", "状态：进行中"))
        self.assertMentions(problems, "本轮状态是 进行中，还没有收尾包")

    def test_round_file_without_round_number_fails(self):
        problems = self.write_round(GOOD_ROUND.replace("轮次：R-900\n", ""))
        self.assertMentions(problems, "轮次")

    def test_round_file_with_unknown_status_fails(self):
        problems = self.write_round(GOOD_ROUND.replace("状态：已收尾", "状态：完成了"))
        self.assertMentions(problems, "状态")


class InFlightSetTest(unittest.TestCase):
    """在途集合只能有一份定义：两个闸门得指向同一个对象（AC-R1-3 / C4）。

    这条用例就是用「两处分别写一份」做反例：那种情况下有人改一处、忘另一处，
    上面那几条事实用例会各自继续绿，而现场又会回到 AW-094 那种两个答案。
    """

    def test_close_gate_reuses_the_work_gate_definition(self):
        self.assertIs(close_gate.IN_FLIGHT_STATES, work_gate.CLOSING_BLOCKING_STATES)

    def test_the_set_is_exactly_these_six_states(self):
        # 逐项写死，两边一起悄悄改也拦得住。
        self.assertEqual(
            ("dispatching", "running", "awaiting_review", "blocked", "ready", "rework"),
            close_gate.IN_FLIGHT_STATES,
        )

    def test_planned_is_not_in_the_set(self):
        self.assertNotIn("planned", close_gate.IN_FLIGHT_STATES)
        self.assertNotIn("planned", work_gate.WAITING_STATES)

    def test_waiting_states_are_a_subset_of_the_set(self):
        self.assertTrue(set(work_gate.WAITING_STATES) <= set(close_gate.IN_FLIGHT_STATES))
        self.assertEqual(("ready", "rework"), work_gate.WAITING_STATES)

    def test_the_documented_six_states_appear_in_the_closing_doc(self):
        """代码与 docs/closing.md 逐项对得上（C5）：文档里那一段列出同一个六个。"""
        text = (ROOT / "docs" / "closing.md").read_text(encoding="utf-8")
        section = text.split("## 在途集合只有一份定义", 1)[1].split("\n## ", 1)[0]
        for state in close_gate.IN_FLIGHT_STATES:
            self.assertIn(state, section, f"文档里少了 {state}")
        self.assertIn("`planned` 不算在途", section)


class FactsTest(unittest.TestCase):
    """`--check` 的三条现场事实：工作区、在途工单、状态与结论一致。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "tasks").mkdir()
        self.round = self.root / "tasks" / "round.md"
        self.pack = self.root / "tasks" / "closing-R-900.md"
        self.round.write_text(GOOD_ROUND, encoding="utf-8")
        self.pack.write_text(GOOD_PACK, encoding="utf-8")
        make_git_repo(self.root)
        git(self.root, "add", "tasks")
        git(self.root, "commit", "-q", "-m", "收尾包")
        # 库放在仓库外面：放进仓库里自己就会变成一处未提交改动，第一条事实永远为红。
        self.dbdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dbdir.cleanup)
        self.db = Path(self.dbdir.name) / "task-center.sqlite3"
        make_db(self.db)

    def facts(self):
        return close_gate.check_facts(self.root, self.db, "test-project")

    def assertMentions(self, problems, fragment):
        self.assertTrue(any(fragment in p for p in problems), problems)

    def test_clean_repo_and_no_in_flight_passes(self):
        self.assertEqual([], self.facts())

    def test_dirty_workspace_fails(self):
        # 用 ASCII 文件名：git 默认把非 ASCII 路径写成 \345 这种转义，断言会扑空。
        (self.root / "dirty-only-on-disk.md").write_text("没进版本库\n", encoding="utf-8")
        problems = self.facts()
        self.assertMentions(problems, "工作区不干净")
        self.assertMentions(problems, "dirty-only-on-disk.md")

    def test_in_flight_ticket_fails(self):
        make_db(self.db, [("AW-999", "test-project", "running")])
        problems = self.facts()
        self.assertMentions(problems, "在途工单")
        self.assertMentions(problems, "AW-999")

    def test_planned_ticket_does_not_block(self):
        """排着的单不挡收尾：`planned` 不在在途集合里（PRD-014 R1 / AC-R1-1）。"""
        make_db(self.db, [("AW-996", "test-project", "planned")])
        self.assertEqual([], self.facts())

    def test_ready_ticket_blocks(self):
        """`ready` 是「等人领走」，仍是没干完的活：钩子一直这么判，闸门得跟上。

        以前闸门只有四个状态，于是 AW-094 留在 `ready` 时 `--check` 说可以收、
        收尾提交被连拒三次：同一件事两个答案。
        """
        make_db(self.db, [("AW-995", "test-project", "ready")])
        problems = self.facts()
        self.assertMentions(problems, "在途工单")
        self.assertMentions(problems, "AW-995")

    def test_rework_ticket_blocks(self):
        make_db(self.db, [("AW-994", "test-project", "rework")])
        problems = self.facts()
        self.assertMentions(problems, "在途工单")
        self.assertMentions(problems, "AW-994")

    def test_planned_and_ready_together_still_block(self):
        """放行的只是 `planned` 那一格：同一张库里还有 ready 就照旧拦。"""
        make_db(self.db, [("AW-993", "test-project", "planned"),
                          ("AW-992", "test-project", "ready")])
        problems = self.facts()
        self.assertMentions(problems, "AW-992")
        self.assertFalse(any("AW-993" in p for p in problems), problems)

    def test_other_project_ticket_does_not_block(self):
        make_db(self.db, [("HP-001", "another-project", "running")])
        self.assertEqual([], self.facts())

    def test_status_and_verdict_must_agree(self):
        self.round.write_text(GOOD_ROUND.replace("状态：已收尾", "状态：等一句答复"), encoding="utf-8")
        problems = self.facts()
        self.assertMentions(problems, "不一致")
        self.assertMentions(problems, "等一句答复")


class CliTest(unittest.TestCase):
    """退出码是闸门的对外契约：0 通过、1 有问题、2 参数或库有问题。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "tasks").mkdir()
        self.round = self.root / "tasks" / "round.md"
        self.pack = self.root / "tasks" / "closing-R-900.md"
        self.round.write_text(GOOD_ROUND, encoding="utf-8")
        self.pack.write_text(GOOD_PACK, encoding="utf-8")

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = close_gate.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_cli_passes_a_sample_package(self):
        code, out, err = self.run_main(["--root", str(self.root)])
        self.assertEqual(0, code, out + err)

    def test_cli_reports_progress_status_and_missing_package(self):
        self.round.write_text(GOOD_ROUND.replace("状态：已收尾", "状态：进行中"), encoding="utf-8")
        self.pack.unlink()
        code, _out, err = self.run_main(["--root", str(self.root)])
        self.assertEqual(1, code)
        self.assertIn("本轮状态是 进行中，还没有收尾包", err)

    def test_cli_usage_error_when_db_missing(self):
        code, _out, err = self.run_main(
            ["--root", str(self.root), "--check", "--db", str(self.root / "nope.sqlite3")]
        )
        self.assertEqual(2, code)
        self.assertIn("中心库不存在", err)


class RealRepoTest(unittest.TestCase):
    """把闸门钉在真实仓库上：状态还是「进行中」时，它必须当场说没收尾。

    读的是**提交过的**内容（`git show HEAD:`），不是工作区那份：`auto_merge`
    合完之后在真实检出上再跑一遍测试，而收尾包可能正写了一半，工作区的内容不该
    决定这一场测试的结果（AW-123）。
    """

    def test_real_round_reports_its_status(self):
        round_text = committed_text(ROOT, "tasks/round.md")
        if round_text is None:
            self.skipTest("取不到 tasks/round.md 的提交内容（不是 git 仓库或缺这一版）")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tasks").mkdir()
            (root / "tasks" / "round.md").write_text(round_text, encoding="utf-8")
            # 已提交的收尾包也一并摆上：「有没有包」不该跟着工作区飘。
            for relpath in committed_closing_packages(ROOT):
                text = committed_text(ROOT, relpath)
                if text is not None:
                    (root / "tasks" / Path(relpath).name).write_text(text, encoding="utf-8")

            round_no, status, problems = close_gate.round_status(root)
            self.assertEqual([], problems)
            self.assertIsNotNone(round_no)
            if status != "进行中":
                self.skipTest(f"本轮状态已是 {status}，收尾包的结构由别的用例覆盖")
            problems = close_gate.check(root)
            self.assertTrue(
                any("还没有收尾包" in p for p in problems), problems
            )


class DefaultDbTest(unittest.TestCase):
    """中心库默认落点：工作树里要退回主检出，否则每个工单都以为自己没有库。"""

    def test_worktree_falls_back_to_main_checkout_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "main"
            root.mkdir()
            make_git_repo(root)
            data = root / ".data"
            data.mkdir()
            make_db(data / "task-center.sqlite3")
            worktree = Path(tmp) / "wt"
            git(root, "worktree", "add", "-q", str(worktree), "-b", "t-wt")
            # git 会回 8.3 短路径（202108~1），比较前先用 resolve() 归一。
            self.assertEqual(
                (data / "task-center.sqlite3").resolve(),
                close_gate.default_db(worktree).resolve(),
            )

    def test_non_git_root_skips_the_git_face(self):
        """业务项目根不是 git 检出时，git 面标跳过而不是判红（AW-153）。

        `台词演进研究` 这类项目根本没有版本库，文档直接落盘。原来 `git status`
        一失败就记一条不通过，那些项目永远卡在收尾闸门外。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tasks").mkdir()
            (root / "tasks" / "round.md").write_text(GOOD_ROUND, encoding="utf-8")
            (root / "tasks" / "closing-R-900.md").write_text(GOOD_PACK, encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = close_gate.main(["--root", str(root), "--check"])
            printed = out.getvalue() + err.getvalue()
            self.assertIn("git 面跳过", printed)
            self.assertNotIn("跑不了 `git status`", printed)
            # 库也不在那边：同样跳过，不当成不通过。
            self.assertIn("库面跳过", printed)
            self.assertEqual(0, code, printed)

    def test_git_root_with_missing_db_still_names_the_first_candidate(self):
        """git 检出上缺库仍然当场报错（退出码 2）：跳过只给不是仓库的项目根。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tasks").mkdir()
            (root / "tasks" / "round.md").write_text(GOOD_ROUND, encoding="utf-8")
            (root / "tasks" / "closing-R-900.md").write_text(GOOD_PACK, encoding="utf-8")
            make_git_repo(root)
            git(root, "add", "tasks")
            git(root, "commit", "-q", "-m", "收尾包")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = close_gate.main(["--root", str(root), "--check"])
            printed = out.getvalue() + err.getvalue()
            self.assertEqual(2, code, printed)
            self.assertIn("中心库不存在", printed)
            self.assertNotIn("库面跳过", printed)

    def test_missing_db_names_the_first_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(root / ".data" / "task-center.sqlite3", close_gate.default_db(root))


if __name__ == "__main__":
    unittest.main()
