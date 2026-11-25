"""行尾自检的用例：受管文本在新检出里必须是 LF。

为什么这些用例要**克隆一份**再验，而不是直接查当前工作树（AW-137 的 C3）：
闸门（`tools/auto_merge.py`）跑测试用的是合并**之前**检出的临时工作树，
执行会话交上来的规则还没进那棵树。拿活工作树当判据，闸门里必红，只能人工合。
所以这里一律 `git clone` 一份到 `.data/lf-check/` 下，设 `core.autocrlf=true`
再检出——那正是「换台机器」的样子，也是这条规则要防的那件事。

第二条用例（删掉规则必须红并点名路径）是 C4 的落点：没有它，「自检绿了」
分不出是规则真的生效，还是自检根本没在查东西。
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import check_eol

# 内容根：本文件在 tools/ 下，往上一级就是内容根。套件仓库里它是 `kit/`，
# 装出来的工作区里它就是工作区根。用例拿它当「要扫的那一棵」。
REPO = Path(__file__).resolve().parent.parent
# 临时克隆放在内容根自己的 .data 下（git 忽略），不用系统临时目录：
# 系统临时目录会被清理，出问题时想回看那份检出不在了。
WORKSPACE = REPO / ".data" / "lf-check"


def git_root(path: Path) -> Path:
    """检出根。**必须分开算**：套件仓库的工作区内容住在 `kit/` 下，`kit/` 自己
    不是一个 git 检出（`.git` 在仓库根），`git clone kit/` 会报
    `does not appear to be a git repository`；装出来的工作区里两者是同一个目录。
    """
    process = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if process.returncode != 0:
        raise unittest.SkipTest(f"{path} 不是一个 git 检出，验不了行尾规则：{process.stderr}")
    return Path(process.stdout.strip()).resolve()


GIT_ROOT = git_root(REPO)
# 内容根在检出根里的位置：套件仓库里是 `kit`，装出来的工作区里是 `.`。
# 扫的是「内容根」，属性文件却读「检出根」那一份——属性按仓库存，不按子目录存。
CONTENT_PREFIX = REPO.resolve().relative_to(GIT_ROOT)


def git(*args, cwd: Path, expect: int | None = 0) -> str:
    """跑一条 git，返回 stdout。expect=None 表示不看退出码。"""
    process = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=600,
    )
    if expect is not None and process.returncode != expect:
        raise AssertionError(
            f"git {' '.join(args)} 在 {cwd} 退出 {process.returncode}（期望 {expect}）\n"
            f"stdout: {process.stdout}\nstderr: {process.stderr}"
        )
    return process.stdout


def fresh_clone(dst: Path):
    """克隆检出根的 HEAD 到 dst，用 core.autocrlf=true 检出——模拟「另一台机器」。

    返回 `(克隆根, 内容根)`：断言里的路径相对内容根，属性文件与 git 命令却属于克隆根。

    先 `--no-checkout` 克隆、改用 true 检出，两步的顺序不能颠倒：先检出再改配置，
    检出来的那一份已经按旧配置写好了，后面改多少配置都不会重写它。
    """
    git("clone", "-q", "--no-checkout", "--no-hardlinks", str(GIT_ROOT), str(dst),
        cwd=dst.parent)
    git("config", "core.autocrlf", "true", cwd=dst)
    sha = git("rev-parse", "HEAD", cwd=GIT_ROOT).strip()
    inside = subprocess.run(["git", "-C", str(dst), "cat-file", "-e", f"{sha}^{{commit}}"],
                            capture_output=True)
    if inside.returncode != 0:
        # 克隆只带走 refs 指得到的对象。如果当前检出是一个游离 HEAD、那个提交又没有
        # 任何分支指着，克隆里就没有它；用 bundle 把 HEAD 单独打出来再克隆，
        # 保证验的还是「这一棵被提交的树」。
        bundle = dst.parent / f"{dst.name}.bundle"
        git("bundle", "create", "--quiet", str(bundle), "HEAD", cwd=GIT_ROOT)
        shutil.rmtree(dst)
        git("clone", "-q", "--no-checkout", str(bundle), str(dst), cwd=dst.parent)
        git("config", "core.autocrlf", "true", cwd=dst)
        sha = git("rev-parse", "HEAD", cwd=dst).strip()
    git("checkout", "-q", "-f", sha, cwd=dst)
    return dst, dst / CONTENT_PREFIX


class WorkspaceCase(unittest.TestCase):
    """每条用例一个自己的临时目录；用完删掉，不留垃圾。"""

    def setUp(self):
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="aw137-", dir=WORKSPACE))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


@unittest.skipUnless(shutil.which("git"), "需要 git 才能验行尾规则")
class FreshCloneTest(WorkspaceCase):
    """C3：受管文本在临时克隆里落地是 LF。"""

    def test_fresh_clone_checks_out_lf(self):
        clone, content = fresh_clone(self.tmp / "lf")
        report = check_eol.scan(content)
        self.assertEqual([], report.problems,
                         "临时克隆里出现了 LF 以外的受管文本：\n" + "\n".join(report.problems))
        self.assertGreater(len(report.managed), 100,
                           f"受管文本只数出 {len(report.managed)} 个，自检大概没在查东西")
        self.assertIn("docs/protocol.md", report.managed)

    def test_c1_three_paths_declare_eol_lf(self):
        clone, content = fresh_clone(self.tmp / "attrs")
        # 三个路径各代表一类：文档、PRD 正文、引擎源码。套件里的 PRD-001 是
        # 《工作区使用与工单体系》（上游那份叫《Cindy 会话工作体系》，名字不同）。
        for path in ("docs/protocol.md", "PRD/PRD-001-工作区使用与工单体系.md",
                     "task_center/core.py"):
            line = git("check-attr", "eol", "--", path, cwd=content).strip()
            self.assertTrue(line.endswith("eol: lf"), f"{path} 的属性是 {line!r}，不是 eol: lf")

    def test_binary_fixture_is_left_alone(self):
        clone, content = fresh_clone(self.tmp / "bin")
        fixture = "tests/fixtures/claims/export.bin"
        inside = (CONTENT_PREFIX / fixture).as_posix() if CONTENT_PREFIX != Path(".") else fixture
        # 二进制「不受影响」只能拿字节说话：它在索引里就是二进制，检出后必须逐字节相同。
        blob = subprocess.run(["git", "-C", str(clone), "show", f"HEAD:{inside}"],
                              capture_output=True, timeout=600).stdout
        self.assertEqual(blob, (content / fixture).read_bytes(),
                         f"{fixture} 被换行转换改写了：二进制必须按原字节取出")
        self.assertNotIn(fixture, check_eol.scan(content).managed,
                         "二进制文件被当成受管文本了，说明 text=auto 没起作用")


@unittest.skipUnless(shutil.which("git"), "需要 git 才能验行尾规则")
class MissingRuleTest(WorkspaceCase):
    """C4：删掉那条规则，自检必须红并点名是哪个路径。"""

    def test_removed_rule_is_reported_with_path(self):
        clone, content = fresh_clone(self.tmp / "norule")
        # 属性文件属于检出根，不是内容根：套件仓库里它在仓库根，内容住在 `kit/` 下。
        attrs = clone / ".gitattributes"
        original = attrs.read_text(encoding="utf-8")
        kept = [line for line in original.splitlines()
                if not (line.strip().startswith("* ") and "eol=lf" in line)]
        self.assertLess(len(kept), len(original.splitlines()),
                        "没找到那条全局规则，这条用例失去意义：请同步 .gitattributes 的写法")
        attrs.write_text("\n".join(kept) + "\n", encoding="utf-8")
        git("add", "-A", cwd=clone)
        git("-c", "user.email=test@example.com", "-c", "user.name=test",
            "commit", "-q", "-m", "删掉全局行尾规则（用例内）", cwd=clone)
        # 规则变了，已检出的文件不会自己重写：清空索引再按新规则重来一遍。
        git("rm", "--cached", "-r", "-q", ".", cwd=clone)
        git("reset", "--hard", "-q", cwd=clone)

        report = check_eol.scan(content)
        self.assertTrue(report.problems, "规则删了，自检却全绿——C4 要的正是这一条红")
        self.assertTrue(any("docs/protocol.md" in problem for problem in report.problems),
                        "自检红了，但没点名受影响的路径：\n" + "\n".join(report.problems))


class PureRulesTest(unittest.TestCase):
    """判行的两条纯规则，不碰 git。"""

    def test_has_crlf(self):
        self.assertTrue(check_eol.has_crlf(b"a\r\nb\r\n"))
        self.assertFalse(check_eol.has_crlf(b"a\nb\n"))
        # 单独的 CR（老式 Mac 行尾）不在这条判据里：git 也只把 CRLF 当行尾。
        self.assertFalse(check_eol.has_crlf(b"a\rb"))

    def test_is_binary(self):
        self.assertTrue(check_eol.is_binary(b"\x89PNG\r\n\x1a\n\x00\x00"))
        self.assertFalse(check_eol.is_binary("# 标题\n\n正文\n".encode("utf-8")))
        # 只看前 8000 字节（与 git 的判定口径一致），后面的 NUL 不算。
        self.assertFalse(check_eol.is_binary(b"a" * 9000 + b"\x00"))


if __name__ == "__main__":
    unittest.main()
