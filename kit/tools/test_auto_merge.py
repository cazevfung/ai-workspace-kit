"""tools/auto_merge.py 自己的用例：五种情况都要断言「目标分支到底动没动」。

AW-018 的验收条件不是「能合并」，而是「不该合并的时候 master 一指头都不能动」。
所以每种情况都用临时 git 仓库跑真实流程，断言的是 commit sha，不是输出里有没有
「失败」两个字：

1. 源分支没领先 → 拒绝；
2. 已跟踪文件有改动（未暂存或已暂存）→ 拒绝；只有未跟踪文件 → 照常合并，
   且输出列出这些文件、说明它们没被合进去（AW-023）；
3. 有冲突 → 中止，master 不动，tag 都还没打；
4. 测试失败 → master 不动，tag 还在，临时 worktree 删干净；
5. 全部通过 → master 前进一个 --no-ff 合并提交，且调用了（假的）merge_ticket；
6. 主检出停在 ticket/AW-0xx → 拒绝（否则 `git merge` 会把 master 合进那条工单分支，
   方向反了），master 与当前分支的 commit 都不变，tag 不打；
7. 锁（AW-027）与锁的作用域（AW-029）：锁在 git 公共目录里，主检出与工作树共用
   同一把——工作树里占着锁时主检出的合并被拒，反过来也一样；
8. 环境异常（AW-032）：git 不在 PATH 里、git 超时、锁目录写不进去，都收成工具
   自己的三行 Fail，且「找不到 git 命令」与「不是 git 仓库」是两句不同的说法。

9. 分支要新增的路径撞上工作区未跟踪文件（AW-050）：在**真合并之前**就停下，
   指名文件、给出补救，master / 回退点 / 合并记录一指头不动，用户文件不删不搬。

10. 半截合并（AW-059）：`git merge` 已经成功、合并记录还没写时，`--record-only`
   把第 2 步的判据反过来问（源分支必须已经全在目标里），在目标的祖先里按
   `parent2` 找那次合并提交，跑全绿才写记录，写成功才清工作树与分支。
   分支还领先、已有记录、测试红、快进历史没有合并提交——四种情形各自拒绝，
   且拒绝时工作树、分支、`merges` 行数都原样。正常合并路径另外写一份
   `.data/merge-journal/<工单号>.json`，写记录成功后删掉。

11. 回执预检（AW-062）：`notices` 为空时正常合并**一步都不动**就退出（`NO_NOTICE`），
   而不是 `git merge` 已经落进目标分支、到第 7 步才被 `merge_ticket` 拒。
   断言的是五样：master、回退点 tag、journal、临时工作树与分支都没变。
   `failed` 也算走过，照样合得下去；`--record-only` 不受这道前置影响。

12. 预演与提示的边界（AW-064）：`--dry-run` 不再被回执预检就地终止，零回执的单
   照样能走到冲突预演与临时 worktree 测试（而同一个单真合并仍被拒）；
   `--record-only` 撞 `NO_NOTICE` 时提示指向「补一条回执」，不再指回命令自己。

临时仓库里放的是真实的 `tools/run_tests.py` 副本，所以「测试失败」这一条跑的是
真的全量测试闸门，不是桩。测试不碰真实仓库、不联网，也不依赖当前分支名。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import auto_merge  # noqa: E402

REAL_RUN_TESTS = HERE / "run_tests.py"

PASSING = """import unittest


class Ok(unittest.TestCase):
    def test_ok(self):
        self.assertTrue(True)
"""

FAILING = """import unittest


class Bad(unittest.TestCase):
    def test_bad(self):
        self.fail("这个用例故意失败，用来验证合并前必须跑绿")
"""


class FakeCenter:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def execute(self, operation, payload, request_id=None):
        self.calls.append((operation, payload, request_id))
        return self.replies[operation]


class RepoFixture(unittest.TestCase):
    """每个用例一个独立的临时仓库：master + ticket/AW-900 分支。

    这里只放夹具与跑流程的小工具，不放用例：继承它的两个类各有用例，
    夹具类自己带用例的话，子类会把它们重跑一遍。
    """

    TICKET = "AW-900"
    BRANCH = "ticket/AW-900"
    TAG = "pre-merge-AW-900"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        # 空全局配置，避免读进本机 core.hooksPath / 默认分支名之类的东西。
        config = self.root / "gitconfig"
        config.write_text("", encoding="utf-8")
        self.env = {
            "GIT_AUTHOR_NAME": "auto merge test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "auto merge test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }

        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "master")
        self.write("tools/run_tests.py", REAL_RUN_TESTS.read_text(encoding="utf-8"))
        # 两处测试目录里的模块名不能重复：unittest discover 按文件名当模块名，
        # 两个 test_ok.py 会让第二处报 ImportError（真实仓库里也是各不重名）。
        self.write("tools/test_gate.py", PASSING)
        self.write("tests/test_sample.py", PASSING)
        self.write("notes.txt", "base\n")
        self.commit("base")

        self.ticket_info = {
            "id": self.TICKET,
            "project_id": "p-test",
            "title": "测试用合并工单",
            "state": "done",
            "merges": [],
            # 回执默认给一条：库里 merge_ticket 本来就要求 notices 非空，夹具单也得有，
            # 否则以后每个「正常路径」用例都在测另一件事（AW-062）。
            "notices": [{"status": "sent", "detail": "已把回执发回原会话"}],
        }
        self.records = []

    # --- 临时仓库的小工具 -------------------------------------------------

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.repo),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, **self.env},
        )

    def write(self, relpath, text):
        path = self.repo / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-m", message)

    def head(self, ref="HEAD"):
        return self.git("rev-parse", ref).stdout.strip()

    def tag_sha(self):
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{self.TAG}"],
            cwd=str(self.repo), capture_output=True, text=True,
            env={**os.environ, **self.env},
        )
        return proc.stdout.strip() if proc.returncode == 0 else None

    def worktree_count(self):
        listing = self.git("worktree", "list", "--porcelain").stdout
        return len([line for line in listing.splitlines() if line.startswith("worktree ")])

    def branch_work(self, files, message="branch work"):
        """在 ticket/AW-900 上提交改动，再切回 master。"""
        self.git("checkout", "-B", self.BRANCH, "master")
        for relpath, text in files.items():
            self.write(relpath, text)
        self.commit(message)
        self.git("checkout", "master")

    # --- 跑一遍流程 -------------------------------------------------------

    def record(self, payload):
        self.records.append(payload)
        return {"merge_id": "merge-1", **payload}

    def run_merge(self, **kwargs):
        chunks = []
        try:
            code = auto_merge.run_merge(
                self.repo,
                self.TICKET,
                branch=self.BRANCH,
                target="master",
                ticket_reader=lambda ticket_id: self.ticket_info,
                merge_recorder=self.record,
                out=chunks.append,
                env=self.env,
                **kwargs,
            )
        except auto_merge.Fail as exc:
            # run_merge 用异常表达「哪一步没过」，main 负责转成退出码并打印补救；这里照做。
            chunks.append(f"合并中止 · {exc.step}\n原因：{exc.reason}\n")
            if exc.fix:
                chunks.append(f"怎么补：{exc.fix}\n")
            code = 1
        return code, "".join(chunks)

    # --- C1：五种情况，每种都断言 master 的 commit -------------------------


class TempRepoCase(RepoFixture):
    def test_not_ahead_is_refused(self):
        """源分支没领先：拒绝，master 不动，连 tag 都不打。"""
        self.git("branch", self.BRANCH, "master")
        before = self.head()
        code, out = self.run_merge()
        self.assertEqual(1, code, out)
        self.assertIn("第 2 步", out)
        self.assertIn("没有领先", out)
        self.assertEqual(before, self.head())
        self.assertIsNone(self.tag_sha())
        self.assertEqual([], self.records)

    def test_head_on_a_ticket_branch_is_refused_in_the_right_direction(self):
        """C2：主检出被切到 ticket/AW-021 时，合并不许跑。

        否则 `git merge --no-ff source` 会把 master 合进 ticket/AW-021：目标分支没
        前进，工单分支被改了，而且看起来像一次成功的合并。这里断言两边 commit 都没变。
        """
        self.branch_work({"feature.txt": "feature\n"})
        self.git("branch", "ticket/AW-021", "master")
        self.git("checkout", "ticket/AW-021")
        master_before = self.head("master")
        current_before = self.head()
        code, out = self.run_merge()
        self.assertEqual(1, code, out)
        self.assertIn("第 2 步", out)
        self.assertIn("git switch master", out)
        self.assertEqual(master_before, self.head("master"), "master 不许动")
        self.assertEqual(current_before, self.head(), "当前分支也不许动")
        self.assertIsNone(self.tag_sha(), "这一步不该打回退点")
        self.assertEqual([], self.records)
        self.assertEqual(1, self.worktree_count())

    def test_dirty_checkout_is_refused(self):
        """检出脏：拒绝，master 不动。"""
        self.branch_work({"feature.txt": "feature\n"})
        before = self.head()
        self.write("notes.txt", "uncommitted local edit\n")
        code, out = self.run_merge()
        self.assertEqual(1, code, out)
        self.assertIn("第 2 步", out)
        self.assertIn("未提交改动", out)
        self.assertEqual(before, self.head())
        self.assertIsNone(self.tag_sha())
        self.assertEqual([], self.records)

    def test_staged_tracked_change_is_refused(self):
        """C1：已跟踪文件有已暂存改动，仍然拒绝，master 不动。

        只放未跟踪文件时不该拦，但把改动 git add 进暂存区就是「要进这次提交」
        的信号，合并会把别人的改动一起带进去，所以照样拒绝。
        """
        self.branch_work({"feature.txt": "feature\n"})
        before = self.head()
        self.write("notes.txt", "staged local edit\n")
        self.git("add", "notes.txt")
        code, out = self.run_merge()
        self.assertEqual(1, code, out)
        self.assertIn("第 2 步", out)
        self.assertIn("未提交改动", out)
        self.assertEqual(before, self.head(), "已跟踪文件有改动时 master 不许动")
        self.assertIsNone(self.tag_sha())
        self.assertEqual([], self.records)

    def test_untracked_files_do_not_block_and_are_reported(self):
        """C2：只有未跟踪文件时照常合并，且输出列出它们、说明没被合进去。

        并行开发线（.agents/skills/skill-routing/、PRD-004、plan-005）留在工作区
        里是常态，它们不参与这次合并，却曾经把合并整体拦住。
        """
        self.branch_work({"feature.txt": "feature\n"})
        self.write("scratch/notes.md", "local only\n")
        self.write("PRD-999-local.md", "local only\n")
        before = self.head()
        code, out = self.run_merge()
        self.assertEqual(0, code, out)
        after = self.head()
        self.assertNotEqual(before, after, "未跟踪文件不许拦住合并")
        self.assertEqual(before, self.head("HEAD^1"))
        self.assertEqual(before, self.tag_sha())
        self.assertEqual(1, len(self.records))
        # 输出要点名列出，并说清它们没有进这次合并
        # （`git status` 默认把未跟踪目录集成为一行，所以这里断言的是目录本身）
        self.assertIn("scratch/", out)
        self.assertIn("PRD-999-local.md", out)
        self.assertIn("没有被合进去", out)
        # 文件本身仍在工作区，且不在合并结果里
        self.assertTrue((self.repo / "scratch/notes.md").is_file(), "未跟踪文件不该被删")
        merged_files = self.git("ls-tree", "-r", "--name-only", after).stdout.split()
        self.assertNotIn("scratch/notes.md", merged_files)
        self.assertNotIn("PRD-999-local.md", merged_files)

    def test_conflict_aborts_without_touching_target(self):
        """两边改同一个文件：中止并列出文件，master 不动，回退点还没建。"""
        self.branch_work({"notes.txt": "branch side\n"})
        self.write("notes.txt", "master side\n")
        self.commit("master side")
        before = self.head()
        code, out = self.run_merge()
        self.assertEqual(1, code, out)
        self.assertIn("第 3 步", out)
        self.assertIn("冲突", out)
        self.assertIn("notes.txt", out)
        self.assertEqual(before, self.head())
        self.assertIsNone(self.tag_sha())
        self.assertEqual(1, self.worktree_count())

    def test_failing_tests_leave_target_and_tag_intact(self):
        """C2：测试失败时 master 指向不变、tag 还在、临时 worktree 删干净。"""
        self.branch_work({"tests/test_bad.py": FAILING})
        before = self.head()
        code, out = self.run_merge()
        self.assertEqual(1, code, out)
        self.assertIn("第 5 步", out)
        self.assertIn("测试失败", out)
        self.assertEqual(before, self.head(), "测试失败时 master 不许动")
        self.assertEqual(before, self.tag_sha(), "回退点必须还在")
        self.assertEqual([], self.records, "失败时不许写合并记录")
        self.assertEqual(1, self.worktree_count(), "临时 worktree 必须删干净")

    def test_success_creates_merge_commit_and_record(self):
        """C3：master 前进一个 --no-ff 合并提交，并把合并记录写回工单。"""
        self.branch_work({"feature.txt": "feature\n"})
        before = self.head()
        code, out = self.run_merge()
        self.assertEqual(0, code, out)
        after = self.head()
        self.assertNotEqual(before, after)
        self.assertEqual(before, self.head("HEAD^1"), "第一个父提交应是合并前的 master")
        parents = self.git("rev-list", "--parents", "-n", "1", after).stdout.split()
        self.assertEqual(3, len(parents), "合并提交必须有两个父提交（--no-ff）")
        self.assertEqual(before, self.tag_sha(), "回退点指向合并前的 master")
        self.assertEqual(1, len(self.records))
        self.assertEqual(after, self.records[0]["commit"])
        self.assertEqual("master", self.records[0]["target_branch"])
        self.assertIn("OK", self.records[0]["tests"], "测试结论应取汇总行")
        self.assertEqual(1, self.worktree_count())

    def test_dry_run_does_not_touch_target(self):
        """dry-run 跑到预演就停：master 不动、不打 tag、不写记录。"""
        self.branch_work({"feature.txt": "feature\n"})
        before = self.head()
        code, out = self.run_merge(dry_run=True)
        self.assertEqual(0, code, out)
        self.assertIn("dry-run", out)
        self.assertEqual(before, self.head())
        self.assertIsNone(self.tag_sha())
        self.assertEqual([], self.records)
        self.assertEqual(1, self.worktree_count())

    # 第 1 步的两道门 ------------------------------------------------------

    def test_ticket_not_done_is_refused(self):
        self.branch_work({"feature.txt": "feature\n"})
        self.ticket_info["state"] = "awaiting_review"
        before = self.head()
        code, out = self.run_merge()
        self.assertEqual(1, code, out)
        self.assertIn("第 1 步", out)
        self.assertIn("done", out)
        self.assertEqual(before, self.head())
        self.assertIsNone(self.tag_sha())

    def test_ticket_already_merged_is_refused(self):
        """第 1 步的「已有合并记录」提示要印出真实 commit_sha 与 target_branch。

        行的形状照 get_ticket 的真实回执来：merges 表的字段是 commit_sha，不是
        commit。旧用例写成 {"commit": ...}，于是提示读 .get("commit") 永远拿到
        None，而测试只断言「不报错、文案里有那四个字」——两个错互相掩盖（AW-024）。
        所以这里既用真实键名，也真的断言提交号出现在文案里。
        """
        self.branch_work({"feature.txt": "feature\n"})
        sha = "7adcfaf0c547bf10e27651d824af6c2971694361"
        self.ticket_info["merges"] = [{"commit_sha": sha, "target_branch": "master"}]
        before = self.head()
        code, out = self.run_merge()
        self.assertEqual(1, code, out)
        self.assertIn("第 1 步", out)
        self.assertIn("已有合并记录", out)
        self.assertIn(sha, out, f"提示里应出现真实提交号，实际输出：{out}")
        self.assertIn("master", out, f"提示里应出现目标分支，实际输出：{out}")
        self.assertNotIn("None", out, f"提示不许把读不到的字段印成 None：{out}")
        self.assertEqual(before, self.head())
        self.assertIsNone(self.tag_sha())


class NoticeGateTest(RepoFixture):
    """回执预检（AW-062）：没过回执的单，一步都不许动。

    以前这道判只在库里的 `merge_ticket`（第 7 步）。AW-061 验收实测过那种现场：
    `git merge` 已经落到 master、`merges` 没有记录、journal 停在 `tests-passed`，
    `--record-only` 又撞回同一句话。所以这里断言的不是「退出码非零」，而是
    **master、tag、journal、工作树、分支五样一个都没动**——那才是「提前」这两个字的意义。
    """

    def test_no_notice_is_refused_before_any_write(self):
        self.branch_work({"feature.txt": "feature\n"})
        self.ticket_info["notices"] = []
        before = self.head()
        branch_tip = self.head(self.BRANCH)
        journal = auto_merge.journal_path(self.repo, self.TICKET)

        code, out = self.run_merge()

        self.assertEqual(1, code, out)
        self.assertIn("第 1 步", out)
        self.assertIn("NO_NOTICE", out, "报的必须是库里那个错误码，两边看到同一句话")
        self.assertIn("notice_payload.py", out, "要指名怎么补")
        self.assertIn("failed", out, "要说明 failed 也算走过回执")
        self.assertNotIn("第 2 步", out, "不许先撞上前置检查——说明它本来就比这一步晚")

        # 五项判据。任何一项变了就说明「提前」只是嘴上说的。
        self.assertEqual(before, self.head(), "master 不许动")
        self.assertIsNone(self.tag_sha(), "回退点 tag 不许打")
        self.assertFalse(journal.exists(), "journal 不许写")
        self.assertEqual(1, self.worktree_count(), "不许建临时工作树")
        self.assertEqual(branch_tip, self.head(self.BRANCH), "源分支不许动")
        self.assertEqual([], self.records, "不许写合并记录")

    def test_dry_run_previews_through_a_ticket_with_no_notice(self):
        """零回执的单，`--dry-run` 要能预演到底，真合并仍被挡住（AW-064 C3–C4）。

        AW-062 验收实测出来的病：回执预检不分路径地 `Fail`，于是 `--dry-run` 在第 1 步
        就地终止了，冲突预演与临时 worktree 里那场全量测试一次都没跑到。
        这里断言两件事同时成立：预演真的走到了第 5 步（“预演通过”），
        而同一个单不加 `--dry-run` 时一步都不动地被拒。
        """
        self.branch_work({"feature.txt": "feature\n"})
        self.ticket_info["notices"] = []
        before = self.head()

        code, out = self.run_merge(dry_run=True)

        self.assertEqual(0, code, out)
        self.assertIn("NO_NOTICE", out, "警告里要点名库里的错误码")
        self.assertIn("预演通过", out, f"--dry-run 必须真跑到临时 worktree 测试：{out}")
        self.assertIn("notice_payload.py", out, "警告里要给出怎么补")
        # 没写任何东西：master、tag、记录、工作树四样。
        self.assertEqual(before, self.head(), "dry-run 不许动 master")
        self.assertIsNone(self.tag_sha(), "dry-run 不许打回退点")
        self.assertEqual([], self.records, "dry-run 不许写合并记录")
        self.assertEqual(1, self.worktree_count(), "临时工作树应该已经删干净")

        # 同一个单真合并：仍然被同一道判拦住，且一步都不动。
        code, out = self.run_merge()
        self.assertEqual(1, code, out)
        self.assertIn("第 1 步", out)
        self.assertIn("NO_NOTICE", out)
        self.assertEqual(before, self.head())
        self.assertIsNone(self.tag_sha())
        self.assertEqual([], self.records)

    def test_missing_notices_key_is_also_refused(self):
        """读到的工单里没有 notices 字段（reader 不完整）也拒：这里宁可变严，
        不能因为读不到就当成「有回执」放过去。"""
        self.branch_work({"feature.txt": "feature\n"})
        del self.ticket_info["notices"]
        before = self.head()

        code, out = self.run_merge()

        self.assertEqual(1, code, out)
        self.assertIn("NO_NOTICE", out)
        self.assertEqual(before, self.head())
        self.assertIsNone(self.tag_sha())

    def test_failed_notice_still_merges(self):
        """`failed` 也算走过回执：回执没送达但原因记下来了，不是能越过去的门。"""
        self.branch_work({"feature.txt": "feature\n"})
        self.ticket_info["notices"] = [{"status": "failed", "detail": "目标会话已归档"}]
        before = self.head()

        code, out = self.run_merge()

        self.assertEqual(0, code, out)
        self.assertIn("status=failed", out, "输出要点名最后一条回执是什么结果")
        self.assertNotEqual(before, self.head())
        self.assertEqual(1, len(self.records))

    def test_record_only_is_not_gated_by_the_notice_precheck(self):
        """`--record-only` 不受这道前置影响：它的适用场景是「已经合了、只差记录」，
        多一道前置只会让恢复更绕；那种现场仍由 `merge_ticket` 拒绝。"""
        self.branch_work({"feature.txt": "feature\n"})
        self.git("merge", "--no-ff", self.BRANCH, "-m", f"Merge {self.BRANCH} into master（工单 {self.TICKET}）")
        commit = self.head()
        self.ticket_info["notices"] = []

        code, out = self.run_merge(record_only=True)

        self.assertEqual(0, code, out)
        self.assertEqual(1, len(self.records), "预检不该把 record-only 拦下来")
        self.assertEqual(commit, self.records[0]["commit"])


class NoticeRecorderHintTest(unittest.TestCase):
    """`--record-only` 撞 `NO_NOTICE` 时，提示要指向「补回执」，不是指向自己（AW-064 C4）。

    拒绝逻辑本身一条都不改（还是库里那道 `NO_NOTICE`），改的只是那句话：人在这种
    现场本来就是因为跑了 `--record-only` 才看到它的，再让他去跑一遍等于没说。
    """

    def test_hint_points_at_notice_payload_instead_of_record_only(self):
        center = mock.Mock()
        center.execute.return_value = {
            "ok": False,
            "error": {"code": "NO_NOTICE", "message": "Ticket has no notice record"},
        }
        recorder = auto_merge.center_merge_recorder(center)
        with self.assertRaises(auto_merge.Fail) as caught:
            recorder({
                "project_id": "p-test", "ticket_id": "AW-900",
                "commit": "0" * 40, "target_branch": "master", "tests": "OK",
            })
        fix = caught.exception.fix
        self.assertIn("notice_payload.py", fix, fix)
        self.assertIn("--ticket AW-900", fix, fix)
        self.assertIn("failed", fix, "要说明 failed 也算走过回执")
        self.assertNotIn("再用 --record-only 收口", fix, "不许指回同一个命令")
        self.assertIn("NO_NOTICE", caught.exception.reason, "拒绝逻辑不改")

    def test_other_rejections_keep_the_record_only_hint(self):
        center = mock.Mock()
        center.execute.return_value = {
            "ok": False,
            "error": {"code": "ALREADY_MERGED", "message": "Already merged"},
        }
        recorder = auto_merge.center_merge_recorder(center)
        with self.assertRaises(auto_merge.Fail) as caught:
            recorder({"project_id": "p-test", "ticket_id": "AW-900", "commit": "0" * 40,
                      "target_branch": "master", "tests": "OK"})
        self.assertIn("--record-only", caught.exception.fix)


class MergeLockTest(RepoFixture):
    """合并互斥：同一时刻只允许一次合并（AW-027）。

    这三条都跑真实的 run_merge：锁是流程的一部分，抽出来测「函数返回什么」
    证明不了「第二个进程会被拦住」，也证明不了失败路径上锁真被释放。
    """

    # 进程表里一定不存在的 pid：Windows 上 OpenProcess 报参数无效，
    # POSIX 上 os.kill 抛 OverflowError（超出 pid_t 范围）。用临时进程的真实
    # pid 反而不稳——它可能立刻被系统复用，测试就会偶发变绿或变红。
    DEAD_PID = 4_000_000_000

    def lock_path(self):
        # 锁在 git 公共目录里，不在检出根——按检出根算会让工作树与主检出各持一把。
        return auto_merge.lock_path(self.repo)

    def write_lock(self, *, pid, ticket, started_at):
        path = self.lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"pid": pid, "ticket": ticket, "started_at": started_at}),
            encoding="utf-8",
        )
        return path

    def now_iso(self, *, minutes_ago=0):
        stamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_second_concurrent_merge_is_refused_and_names_the_occupant(self):
        """C1：已有一次合并在跑时，第二次必须被拒，且输出能直接看出占用者。

        断言的是四样东西：非零退出、工单号、pid、开始时间与已持续多久。
        还要断言它**不是**撞在第 1、2 步上——那说明锁根本没拦住，只是恰好被
        别的检查挡下了，看起来一样但下次就不一定。
        """
        self.branch_work({"feature.txt": "feature\n"})
        started = self.now_iso(minutes_ago=3)
        self.write_lock(pid=os.getpid(), ticket="AW-026", started_at=started)
        before = self.head()

        code, out = self.run_merge(dry_run=True)

        self.assertEqual(1, code, out)
        self.assertIn("第 0 步", out, "拒绝必须发生在第 1 步之前")
        self.assertNotIn("第 1 步", out, "不该先撞上读工单那一步")
        self.assertIn("AW-026", out, "输出要点名占用者正在合哪张单")
        self.assertIn(str(os.getpid()), out, "输出要带占用者 pid")
        self.assertIn(started, out, "输出要带占用者开始时间")
        self.assertIn("已持续", out, "输出要带已持续多久")
        self.assertEqual(before, self.head(), "被拒时 master 不许动")
        self.assertIsNone(self.tag_sha(), "被拒时连回退点都不该打")
        self.assertEqual([], self.records)

    def test_stale_lock_is_taken_over_and_reported(self):
        """C2：锁的持有者已经不在了，下一次合并要接管它并说明接的是哪把锁。

        残留锁是自己造成的麻烦：进程被强杀时 finally 跑不到，锁就留在那里。
        不接管就等于「一次崩溃让合并永久停摆」，这与「锁不永久残留」相矛盾。
        """
        self.branch_work({"feature.txt": "feature\n"})
        old_started = "2026-11-16T01:02:03Z"
        path = self.write_lock(pid=self.DEAD_PID, ticket="AW-026", started_at=old_started)
        before = self.head()

        code, out = self.run_merge(dry_run=True)

        self.assertEqual(0, code, out)
        self.assertIn("接管", out, "输出要写明接管了残留锁")
        self.assertIn("AW-026", out, "要说明接的是哪张单留下的锁")
        self.assertIn(str(self.DEAD_PID), out, "要说明那把锁的 pid")
        self.assertIn(old_started, out, "要说明那把锁的开始时间")
        self.assertEqual(before, self.head())
        self.assertFalse(path.exists(), "接管之后旧锁文件必须换成本进程的新锁，再被释放")

    def test_lock_is_released_when_merge_fails(self):
        """C2 的另一半：异常路径也要释放，否则一次红灯就永久占住合并。"""
        self.branch_work({"tests/test_bad.py": FAILING})

        code, out = self.run_merge()

        self.assertEqual(1, code, out)
        self.assertIn("取到锁", out, "先确认这次确实取过锁，否则「没残留」是空的")
        self.assertFalse(self.lock_path().exists(), "失败时也要释放互斥锁")


class SharedLockScopeTest(RepoFixture):
    """锁的作用域是整个仓库，不是「当前检出的仓库根」（AW-029）。

    AW-027 的锁按检出根算路径：主检出的 `.git` 是目录，工作树的 `.git` 是个文件，
    于是工作树里放一把活锁时，主检出的合并照样能取到自己的锁——两把互不拦，
    当时只靠「合并都从主检出发起」这个习惯在挡。这三条钉的就是这件事。
    """

    WT_BRANCH = "ticket/AW-901"

    def add_worktree(self):
        wt = self.root / "wt"
        self.git("worktree", "add", "-b", self.WT_BRANCH, str(wt), "master")
        self.addCleanup(self.drop_worktree, wt)
        return wt

    def drop_worktree(self, wt):
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt)],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            env={**os.environ, **self.env},
        )

    def now_iso(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def hold_lock(self, where, ticket="AW-026"):
        """在 where 那把锁的位置放一把活锁：pid 是本进程，所以不会被判为残留。"""
        path = auto_merge.lock_path(where)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"pid": os.getpid(), "ticket": ticket, "started_at": self.now_iso()}),
            encoding="utf-8",
        )
        return path

    def run_merge_in(self, where):
        """在指定检出（主检出或工作树）上跑一遍合并流程，只看第 0 步的结论。"""
        chunks = []
        try:
            code = auto_merge.run_merge(
                where,
                self.TICKET,
                branch=self.BRANCH,
                target="master",
                dry_run=True,
                ticket_reader=lambda ticket_id: self.ticket_info,
                merge_recorder=self.record,
                out=chunks.append,
                env=self.env,
            )
        except auto_merge.Fail as exc:
            chunks.append(f"合并中止 · {exc.step}\n原因：{exc.reason}\n")
            code = 1
        return code, "".join(chunks)

    def test_main_checkout_and_worktree_share_one_absolute_lock_path(self):
        """C2：主检出与工作树算出的锁路径必须是同一个绝对路径。"""
        wt = self.add_worktree()
        main_lock = auto_merge.lock_path(self.repo)
        wt_lock = auto_merge.lock_path(wt)
        self.assertTrue(main_lock.is_absolute(), main_lock)
        self.assertTrue(wt_lock.is_absolute(), wt_lock)
        self.assertEqual(main_lock, wt_lock, "两边必须是同一把锁，否则互斥是假的")
        self.assertNotEqual(self.repo / ".data" / "auto-merge.lock", main_lock,
                            "锁不该还留在检出根下")

    def test_lock_held_in_worktree_blocks_merge_from_main_checkout(self):
        """C1a：工作树里占着锁 → 主检出起合并被拒。

        主检出停在 master 上、源分支也领先，所以「被拒」只可能是因为锁：
        按检出根算路径时这条会失败，主检出的合并会真的跑下去（并成功）。
        """
        self.branch_work({"feature.txt": "feature\n"})
        wt = self.add_worktree()
        path = self.hold_lock(wt, ticket="AW-026")
        started = json.loads(path.read_text(encoding="utf-8"))["started_at"]
        before = self.head()

        code, out = self.run_merge_in(self.repo)

        self.assertEqual(1, code, out)
        self.assertIn("第 0 步", out, "拒绝必须发生在第 1 步之前")
        self.assertNotIn("第 1 步", out)
        self.assertIn("AW-026", out, "输出要点名占用者正在合哪张单")
        self.assertIn(str(os.getpid()), out, "输出要带占用者 pid")
        self.assertIn(started, out, "输出要带占用者开始时间")
        self.assertIn("已持续", out, "输出要带已持续多久")
        self.assertIn(path.as_posix(), out, "输出要带锁的绝对路径")
        self.assertTrue(path.is_file(), "被拒的一方不许动别人手里的锁")
        self.assertEqual(before, self.head(), "被拒时 master 不许动")
        self.assertIsNone(self.tag_sha(), "被拒时连回退点都不该打")
        self.assertEqual([], self.records)

    def test_lock_held_in_main_checkout_blocks_merge_from_worktree(self):
        """C1b：反方向——主检出占着锁 → 工作树起合并同样被拒。

        工作树停在 ticket/AW-901 上，如果锁没拦住，这里会撞到第 2 步的「不是目标
        分支」；断言停在第 0 步、且没有第 2 步，才说明拦住它的是锁而不是别的检查。
        """
        self.branch_work({"feature.txt": "feature\n"})
        wt = self.add_worktree()
        path = self.hold_lock(self.repo, ticket="AW-027")
        started = json.loads(path.read_text(encoding="utf-8"))["started_at"]

        code, out = self.run_merge_in(wt)

        self.assertEqual(1, code, out)
        self.assertIn("第 0 步", out)
        self.assertNotIn("第 2 步", out, "不该先撞上「检出不在目标分支」那一步")
        self.assertIn("AW-027", out)
        self.assertIn(str(os.getpid()), out)
        self.assertIn(started, out)
        self.assertIn(path.as_posix(), out)
        self.assertTrue(path.is_file(), "被拒的一方不许动别人手里的锁")


class EnvironmentFailureTest(RepoFixture):
    """环境异常时也要说人话：找不到 git、git 超时、公共目录不可写（AW-032）。

    AW-029 的独立验收实测出来：把 git 从 PATH 摘掉跑 --dry-run，第 0 步的
    `git_common_dir` 丢出一个没人接的 FileNotFoundError，打出一整段 traceback；
    更糟的是它原来那句提示会把「找不到 git」误诊成「不是 git 仓库」。

    这里钉两件事：异常要收成本工具那套三行 Fail（中止 · 原因 · 怎么补），
    以及「找不到 git 命令」与「不是 git 仓库」必须是两句不同的话、各有各的补救。
    """

    def three_lines(self, out):
        """三行 Fail 的形状：第一行说停在哪一步，第二行原因，第三行怎么补。"""
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertTrue(lines and lines[0].startswith("合并中止 · "), out)
        self.assertTrue(any(line.startswith("原因：") for line in lines), out)
        self.assertTrue(any(line.startswith("怎么补：") for line in lines), out)
        return lines

    def run_merge_with_env(self, env):
        chunks = []
        try:
            code = auto_merge.run_merge(
                self.repo,
                self.TICKET,
                branch=self.BRANCH,
                target="master",
                dry_run=True,
                ticket_reader=lambda ticket_id: self.ticket_info,
                merge_recorder=self.record,
                out=chunks.append,
                env=env,
            )
        except auto_merge.Fail as exc:
            chunks.append(f"合并中止 · {exc.step}\n原因：{exc.reason}\n怎么补：{exc.fix}\n")
            code = 1
        return code, "".join(chunks)

    def test_missing_git_gives_three_lines_not_a_traceback(self):
        """C1：git 不在 PATH 里时，跑 --dry-run 得到三行提示，不是 traceback。

        第 0 步要算锁放在哪里，一碰 git 就是 FileNotFoundError。断言：退出码非零、
        三行形状、说「找不到 git 命令」、没有 traceback；再加一条反向断言——不许
        说「不是 git 仓库」，那是把「没装 git」误诊成「目录选错」。

        改的是本进程的 PATH，不是子进程 env 里的 PATH：Windows 上 CreatProcess 用
        **调用方进程**的 PATH 找可执行文件，子进程环境块里的 PATH 改不掉这一点。
        """
        self.branch_work({"feature.txt": "feature\n"})
        empty_bin = self.root / "empty-bin"
        empty_bin.mkdir()
        before = self.head()

        with mock.patch.dict(os.environ, {"PATH": str(empty_bin)}):
            code, out = self.run_merge_with_env(self.env)

        self.assertEqual(1, code, out)
        self.three_lines(out)
        self.assertIn("找不到 git 命令", out)
        self.assertIn("PATH", out, "补救句要指向 PATH")
        self.assertIn("git --version", out, "补救句要能给出一条可执行的检查命令")
        self.assertNotIn("Traceback", out, "环境异常不许把 traceback 打在人脸上")
        self.assertNotIn("FileNotFoundError", out)
        self.assertNotIn("不是 git 仓库", out, "找不到 git 不等于目录选错，不许误诊")
        self.assertIn("第 0 步", out, "锁都算不出路径，就该停在第 0 步")
        self.assertEqual(before, self.head(), "环境异常时目标分支不许动")
        self.assertIsNone(self.tag_sha())
        self.assertEqual([], self.records)

    def test_not_a_repo_and_missing_git_are_two_different_sentences(self):
        """C2：两种情形可区分，且各有各的补救句。

        这两句话混用的后果不对称：把「没装 git」说成「不是 git 仓库」，人会去翻
        目录（翻不到）；反过来会把目录问题当成安装问题（重装也没用）。
        """
        not_repo = self.root / "plain-dir"
        not_repo.mkdir()
        # 不许 git 往上翻到别的仓库（临时目录可能在某个仓库里面）。
        env = {**self.env, "GIT_CEILING_DIRECTORIES": str(self.root)}
        with self.assertRaises(auto_merge.Fail) as caught:
            auto_merge.git_common_dir(not_repo, env=env)
        missing_dir = caught.exception
        self.assertIn("不是 git 仓库", missing_dir.reason)
        self.assertIn("在仓库根跑", missing_dir.fix)
        self.assertNotIn("找不到 git", missing_dir.reason)

        empty_bin = self.root / "empty-bin"
        empty_bin.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty_bin)}):
            with self.assertRaises(auto_merge.Fail) as caught:
                auto_merge.git_common_dir(not_repo, env=env)
        no_git = caught.exception
        self.assertIn("找不到 git 命令", no_git.reason)
        self.assertIn("PATH", no_git.fix)
        self.assertNotIn("不是 git 仓库", no_git.reason)

        self.assertNotEqual(no_git.reason, missing_dir.reason, "两种情形必须是两种说法")
        self.assertNotEqual(no_git.fix, missing_dir.fix, "补救句也不能共用")

    def test_missing_git_in_any_git_call_becomes_a_fail(self):
        """C1：第 0 步以外的 git 调用同样收口——不是只在算锁路径那一处挡一下。

        摘掉 git 后能跑到后面几步的情况是存在的（比如锁已经在手上），所以 `git()`
        自己也必须把 FileNotFoundError 转成三行 Fail，而不是让它冒到 main 外面。
        """
        boom = FileNotFoundError(2, "The system cannot find the file specified")
        with mock.patch.object(auto_merge.subprocess, "run", side_effect=boom):
            with self.assertRaises(auto_merge.Fail) as caught:
                auto_merge.git(self.repo, "status", "--porcelain", step=auto_merge.STEP_CHECKS)
        self.assertEqual(auto_merge.STEP_CHECKS, caught.exception.step)
        self.assertIn("找不到 git 命令", caught.exception.reason)
        self.assertIn("PATH", caught.exception.fix)

    def test_git_timeout_becomes_a_fail(self):
        """git 卡住不动：转成三行 Fail 并说明怎么查，不是等两个小时。"""
        expired = subprocess.TimeoutExpired(cmd="git status", timeout=600)
        with mock.patch.object(auto_merge.subprocess, "run", side_effect=expired):
            with self.assertRaises(auto_merge.Fail) as caught:
                auto_merge.git(self.repo, "status", step=auto_merge.STEP_CHECKS)
        self.assertIn("600", caught.exception.reason, "原因里要带上是等了多少秒")
        self.assertIn("没有返回", caught.exception.reason)
        self.assertIn("index.lock", caught.exception.fix)

    def test_unwritable_lock_dir_becomes_a_fail_not_a_traceback(self):
        """公共目录不可写：OSError 也要收成三行 Fail，并点名写不进哪条路径。

        注意这条只证明「转换」这一层（用打桩让 os.open 报权限错），没有真的把
        .git 设成只读——那件事本机复现不了，写在 risks 里。
        """
        denied = PermissionError(13, "Permission denied")
        chunks = []
        with mock.patch("auto_merge.os.open", side_effect=denied):
            with self.assertRaises(auto_merge.Fail) as caught:
                auto_merge.acquire_merge_lock(self.repo, self.TICKET, chunks.append, env=self.env)
        self.assertEqual(auto_merge.STEP_LOCK, caught.exception.step)
        self.assertIn("写不进去", caught.exception.reason)
        self.assertIn("auto-merge.lock", caught.exception.reason, "要点名是哪条路径")
        self.assertIn("可写", caught.exception.fix)


class UntrackedOverlapTest(unittest.TestCase):
    """纯函数部分（AW-050）：分隔符与大小写不是差别，不同路径要放过。"""

    def test_separator_and_case_are_not_a_difference(self):
        """反斜杠与大小写都是同一个人在 Windows 上写同一个路径的两种写法。"""
        added = ["PRD/PRD-008-批量迁移技能内容.md"]
        untracked = ["prd\\PRD-008-批量迁移技能内容.MD"]
        self.assertEqual(untracked, auto_merge.untracked_overlap(added, untracked))

    def test_hit_is_reported_with_the_name_in_the_worktree(self):
        """报给用户的得是工作区里那个名字——他要按它去挪文件。"""
        hits = auto_merge.untracked_overlap(["PRD/A.md"], ["PRD/a.md"])
        self.assertEqual(["PRD/a.md"], hits)

    def test_different_paths_are_untouched(self):
        self.assertEqual([], auto_merge.untracked_overlap(
            ["feature.txt", "PRD/a.md"], ["notes.txt", "PRD/b.md", ".data/x"],
        ))

    def test_leading_dot_slash_and_trailing_slash_are_normalised(self):
        self.assertEqual(
            ["./PRD/a.md"],
            auto_merge.untracked_overlap(["PRD/a.md"], ["./PRD/a.md"]),
        )

    def test_duplicate_hits_are_collapsed(self):
        hits = auto_merge.untracked_overlap(["a.md", "./A.MD"], ["a.md"])
        self.assertEqual(["a.md"], hits)


class UntrackedOverwriteTest(RepoFixture):
    """C2：分支要新增的路径与工作区未跟踪文件撞名时，**在合并之前**就停下。

    AW-049 实况：那份未跟踪文件与分支上的新文件逐字节相同，git merge 仍拒绝。
    所以这里也拿相同内容造场景——靠比内容放过是错的。
    """

    NAME = "PRD/PRD-008-批量迁移技能内容.md"

    def make_collision(self, untracked_name=None):
        self.branch_work({self.NAME: "同样的内容\n"})
        self.write(untracked_name or self.NAME, "同样的内容\n")

    def test_identical_untracked_file_stops_before_merge(self):
        self.make_collision()
        before = self.head()
        code, out = self.run_merge()
        self.assertEqual(1, code, out)
        # 停在预检，不是停在真合并
        self.assertIn(auto_merge.STEP_UNTRACKED, out)
        self.assertNotIn("第 6 步", out)
        # 指名那个文件（而不是一句原始 git 报错）
        self.assertIn(self.NAME, out)
        # 补救得能照做，且不许把人推向系统 Temp
        self.assertIn(".data/", out)
        self.assertIn("evidence", out)
        self.assertIn("Temp", out)
        self.assertIn("只看只报", out)
        # 仓库一指头没动：master 不动、回退点没打、没写记录、没留临时 worktree
        self.assertEqual(before, self.head())
        self.assertIsNone(self.tag_sha())
        self.assertEqual([], self.records)
        self.assertEqual(1, self.worktree_count())
        # 只看只报：用户的文件原地不动，内容也不变
        kept = self.repo / self.NAME
        self.assertTrue(kept.is_file(), "不允许删或搬用户的未跟踪文件")
        self.assertEqual("同样的内容\n", kept.read_text(encoding="utf-8"))

    def test_case_variant_untracked_file_also_stops(self):
        """Windows 上不分大小写：`prd-008.md` 也是同一个路径。"""
        self.make_collision("PRD/prd-008-批量迁移技能内容.md")
        before = self.head()
        code, out = self.run_merge()
        self.assertEqual(1, code, out)
        self.assertIn(auto_merge.STEP_UNTRACKED, out)
        self.assertIn("prd-008", out.lower())
        self.assertEqual(before, self.head())
        self.assertIsNone(self.tag_sha())

    def test_directory_neighbours_do_not_collide(self):
        """同一个目录里的别的未跟踪文件不算撞名：合并照常成功，两边文件都在。"""
        self.branch_work({"PRD/a.md": "branch\n"})
        self.write("PRD/b.md", "local only\n")
        before = self.head()
        code, out = self.run_merge()
        self.assertEqual(0, code, out)
        after = self.head()
        self.assertNotEqual(before, after)
        merged = self.git("ls-tree", "-r", "--name-only", after).stdout.split()
        self.assertIn("PRD/a.md", merged)
        self.assertNotIn("PRD/b.md", merged)
        self.assertTrue((self.repo / "PRD/b.md").is_file(), "未跟踪文件不该被删")


class CenterBridgeTest(unittest.TestCase):
    """默认的工单读取与合并记录写入：只验 envelope，不连真实库。"""

    def test_reader_returns_ticket_and_reports_missing(self):
        center = FakeCenter({"get_ticket": {"ok": True, "result": {"id": "AW-900", "state": "done"}}})
        read = auto_merge.center_ticket_reader(center, "p-test")
        self.assertEqual("AW-900", read("AW-900")["id"])
        self.assertEqual("get_ticket", center.calls[0][0])
        self.assertEqual({"project_id": "p-test", "ticket_id": "AW-900"}, center.calls[0][1])

        missing = FakeCenter({"get_ticket": {"ok": False, "error": {"code": "NOT_FOUND", "message": "x"}}})
        read = auto_merge.center_ticket_reader(missing, "p-test")
        with self.assertRaises(auto_merge.Fail) as caught:
            read("AW-900")
        self.assertEqual(auto_merge.STEP_TICKET, caught.exception.step)

    def test_recorder_writes_merge_ticket_envelope(self):
        center = FakeCenter({"merge_ticket": {"ok": True, "result": {"merge_id": "m-1"}}})
        record = auto_merge.center_merge_recorder(center)
        payload = {
            "project_id": "p-test", "ticket_id": "AW-900", "commit": "a" * 40,
            "target_branch": "master", "tests": "tests/ 1 OK | 合计 1 项",
        }
        self.assertEqual("m-1", record(payload)["merge_id"])
        operation, sent, request_id = center.calls[0]
        self.assertEqual("merge_ticket", operation)
        self.assertEqual(payload, sent)
        self.assertEqual("auto-merge-AW-900", request_id)

    def test_recorder_reports_refusal_as_step_seven(self):
        center = FakeCenter({"merge_ticket": {"ok": False, "error": {"code": "ALREADY_MERGED", "message": "已有"}}})
        with self.assertRaises(auto_merge.Fail) as caught:
            auto_merge.center_merge_recorder(center)({"ticket_id": "AW-900"})
        self.assertEqual(auto_merge.STEP_RECORD, caught.exception.step)
        self.assertIn("ALREADY_MERGED", caught.exception.reason)


class ListUnmergedTest(unittest.TestCase):
    def test_lists_done_but_unmerged(self):
        center = FakeCenter({"list_unmerged": {"ok": True, "result": {"items": [
            {"id": "AW-014", "project_id": "ai-workspace", "title": "某工单"},
        ], "count": 1}}})
        chunks = []
        items = auto_merge.list_unmerged_rows(center, chunks.append)
        text = "".join(chunks)
        self.assertEqual(1, len(items))
        self.assertIn("AW-014", text)
        self.assertIn("auto_merge.py --ticket", text)

    def test_empty_list_says_so(self):
        center = FakeCenter({"list_unmerged": {"ok": True, "result": {"items": [], "count": 0}}})
        chunks = []
        self.assertEqual([], auto_merge.list_unmerged_rows(center, chunks.append))
        self.assertIn("没有", "".join(chunks))


class RecordOnlyTest(RepoFixture):
    """`--record-only`：收口「合并已成功、记录还没写」的半截现场（AW-059）。

    这四条对应计划里的验证清单 (a)~(d)，再加一条「根本找不到合并提交」。
    每条都断言**库里到底有没有多出记录**、**工作树与分支还在不在**：
    工具说「成功」不算证据，证据是那两件事的状态。
    """

    def merged_without_record(self):
        """造出半截现场：真合一次，工作树与分支都留着（模拟断在写记录之前）。"""
        self.branch_work({"feature.txt": "feature\n"})
        self.git("merge", "--no-ff", self.BRANCH, "-m", f"Merge {self.BRANCH} into master（工单 {self.TICKET}）")
        commit = self.head()
        self.git("worktree", "add", str(self.repo / ".worktrees" / self.TICKET), self.BRANCH)
        return commit

    def worktree_dir(self):
        return self.repo / ".worktrees" / self.TICKET

    def branch_exists(self, name=None):
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name or self.BRANCH}"],
            cwd=str(self.repo), capture_output=True, text=True,
            env={**os.environ, **self.env},
        )
        return proc.returncode == 0

    def test_records_then_cleans_up(self):
        """(a) 已合并且无记录：跑测试 → 写记录 → 工作树与分支都清掉。"""
        commit = self.merged_without_record()
        self.assertTrue(self.worktree_dir().is_dir())
        code, out = self.run_merge(record_only=True)
        self.assertEqual(0, code, out)
        self.assertIn(commit[:12], out, "输出要点名那次真实的合并提交")
        self.assertEqual(1, len(self.records), out)
        self.assertEqual(commit, self.records[0]["commit"])
        self.assertEqual("master", self.records[0]["target_branch"])
        self.assertEqual("p-test", self.records[0]["project_id"])
        self.assertIn("OK", self.records[0]["tests"], "tests 要取这次实跑的汇总行")
        self.assertEqual(commit, self.head(), "--record-only 不许动目标分支")
        self.assertFalse(self.worktree_dir().exists(), "工作树没清掉")
        self.assertFalse(self.branch_exists(), "分支没清掉")
        self.assertIn("清理：已删除工作树", out)
        self.assertIn("清理：已删除分支", out)

    def test_branch_still_ahead_is_refused(self):
        """(b) 分支还领先（其实是没合）：拒绝，什么都不动。"""
        self.branch_work({"feature.txt": "feature\n"})
        before = self.head()
        code, out = self.run_merge(record_only=True)
        self.assertEqual(1, code, out)
        self.assertIn("第 2 步", out)
        self.assertIn("已经全在", out)
        self.assertEqual(before, self.head())
        self.assertEqual([], self.records)
        self.assertTrue(self.branch_exists())

    def test_existing_record_reports_already_merged(self):
        """(c) 已有记录：报 ALREADY_MERGED，不重复写、不清场。"""
        commit = self.merged_without_record()
        self.ticket_info["merges"] = [{"commit_sha": commit, "target_branch": "master"}]
        code, out = self.run_merge(record_only=True)
        self.assertEqual(1, code, out)
        self.assertIn("第 1 步", out)
        self.assertIn("ALREADY_MERGED", out)
        self.assertEqual([], self.records)
        self.assertTrue(self.worktree_dir().is_dir(), "拒绝时不许清场")
        self.assertTrue(self.branch_exists())

    def test_red_tests_leave_worktree_and_branch_alone(self):
        """(d) 测试红：非零退出，merge 行数不变，工作树与分支都还在。"""
        commit = self.merged_without_record()
        # 合并之后 master 上再落一个必然红的用例：这就是「收口时测试不过」的现场。
        self.write("tests/test_bad.py", FAILING)
        self.commit("red on master")
        red = self.head()
        code, out = self.run_merge(record_only=True)
        self.assertEqual(1, code, out)
        self.assertIn("第 5 步", out)
        self.assertIn("测试失败", out)
        self.assertIn("没有写合并记录", out)
        self.assertEqual([], self.records, "红的时侯一条记录都不许写")
        self.assertEqual(red, self.head(), "测试红时目标分支也不许动")
        self.assertTrue(self.worktree_dir().is_dir(), "红的时侯不许清场")
        self.assertTrue(self.branch_exists())
        # 目标分支已经前进过：要找的仍然必须是那次合并提交，不是现在的 HEAD。
        self.assertNotEqual(commit, red)
        self.assertIn(commit[:12], out, "要找的是那次合并提交，不是当前 HEAD")

    def test_fast_forward_history_is_refused(self):
        """没有合并提交（快进）时不写记录：记录里的 commit 不能拿 HEAD 顶替。"""
        self.branch_work({"feature.txt": "feature\n"})
        self.git("merge", "--ff-only", self.BRANCH, "-m", "fast forward")
        before = self.head()
        code, out = self.run_merge(record_only=True)
        self.assertEqual(1, code, out)
        self.assertIn("找合并提交", out)
        self.assertIn("parent2", out)
        self.assertEqual([], self.records)
        self.assertEqual(before, self.head())
        self.assertTrue(self.branch_exists())


class MergeJournalTest(RepoFixture):
    """合并进程记录（`.data/merge-journal/<工单号>.json`）：断了能看出做到第几步。"""

    def test_steps_are_written_then_removed_on_success(self):
        self.branch_work({"feature.txt": "feature\n"})
        seen = []
        real_write = auto_merge.write_journal

        def spy(repo, ticket_id, payload, emit=None):
            seen.append(dict(payload))
            return real_write(repo, ticket_id, payload, emit=emit)

        with mock.patch.object(auto_merge, "write_journal", side_effect=spy):
            code, out = self.run_merge()
        self.assertEqual(0, code, out)
        self.assertEqual(["merged", "tests-passed"], [item["step"] for item in seen], out)
        first = seen[0]
        self.assertEqual(self.head(), first["commit"])
        self.assertEqual("master", first["target_branch"])
        self.assertEqual(self.BRANCH, first["source_branch"])
        self.assertEqual(self.TAG, first["tag"])
        self.assertIn("started_at", first)
        self.assertFalse(
            auto_merge.journal_path(self.repo, self.TICKET).exists(),
            "写记录成功后进程记录必须删掉",
        )

    def test_clear_journal_marks_rolled_back_before_deleting(self):
        auto_merge.write_journal(self.repo, self.TICKET, {"step": "merged", "commit": "x"})
        path = auto_merge.journal_path(self.repo, self.TICKET)
        self.assertTrue(path.is_file())
        seen = []
        real_write = auto_merge.write_journal

        def spy(repo, ticket_id, payload, emit=None):
            seen.append(dict(payload))
            return real_write(repo, ticket_id, payload, emit=emit)

        with mock.patch.object(auto_merge, "write_journal", side_effect=spy):
            auto_merge.clear_journal(self.repo, self.TICKET, mark="rolled-back")
        self.assertFalse(path.exists(), "回滚后进程记录也要删掉")
        self.assertEqual("rolled-back", seen[-1]["step"])

    def test_failing_preview_never_writes_journal(self):
        """预演就红时根本没合，也就没有进程记录——它不是「合并失败」记录。"""
        self.branch_work({"tests/test_bad.py": FAILING})
        code, out = self.run_merge()
        self.assertEqual(1, code, out)
        self.assertFalse(auto_merge.journal_path(self.repo, self.TICKET).exists())


if __name__ == "__main__":
    unittest.main()
