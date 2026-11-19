#!/usr/bin/env python3
"""check_index.py 的反例用例：每一类错都必须真的变红，还原后必须真的变绿。

判据换了之后仍然要盯住两件事：description 只归 `approved` 管（偏离就报
`description-drifted`），正文只归快照管（改一行就报 `body-changed`），两者不能互相
顶替。技能个数从词表读出，所以词表一变、报表里的数字必须跟着变。

跑法（直接跑文件，不接进 tools/run_tests.py —— 那一层明文不扫 .agents/）：

    python .agents/skills/skill-routing/scripts/test_check_index.py
    python .agents/skills/skill-routing/scripts/test_check_index.py -v

前半段是拿本机 live 文件跑基线；后半段把 live 文件与 index 拷进临时目录再故意改坏，
所以不会碰到真的 `~/.agents/skills`。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check_index.py"
REAL_ROUTING = SCRIPT.parent.parent
REAL_INDEX = REAL_ROUTING / "index"
REAL_SKILLS = Path.home() / ".agents" / "skills"
SAMPLE = "test-driven-development"      # 被反复改坏的那一个
BODY_SAMPLE = "writing-plans"

for _stream in (sys.stdout, sys.stderr):  # 控制台按 utf-8 输出，否则中文证据看不懂
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def describe(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("description:"):
            return line.strip()[len("description:"):].strip()
    raise AssertionError("%s 里没有 description 行" % path)


def rewrite_description(path: Path, text: str) -> None:
    lines = path.read_bytes().decode("utf-8").splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.lstrip().startswith("description:"):
            eol = "\r\n" if line.endswith("\r\n") else "\n"
            lines[i] = "description: " + text + eol
            break
    else:
        raise AssertionError("%s 里没有 description 行" % path)
    path.write_bytes("".join(lines).encode("utf-8"))


class CheckerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)

        expected = json.loads((REAL_INDEX / "expected-terms.json").read_text(encoding="utf-8"))
        self.names = [item["name"] for item in expected["skills"]]

        self.skills = self.work / "skills"
        self.skills.mkdir()
        for name in self.names:
            shutil.copytree(REAL_SKILLS / name, self.skills / name)

        self.index = self.work / "index"
        shutil.copytree(REAL_INDEX, self.index)

        # 只需要处置表，不必整份 routing 目录（里面有 index/ 自己）
        self.routing = self.work / "routing"
        self.routing.mkdir()
        shutil.copyfile(REAL_ROUTING / "installed-skills.md", self.routing / "installed-skills.md")

    def run_check(self, only: str | None = None) -> tuple[int, dict, str]:
        command = [sys.executable, str(SCRIPT),
                   "--skills-root", str(self.skills),
                   "--index-root", str(self.index),
                   "--routing-dir", str(self.routing),
                   "--json"]
        if only:
            command += ["--only", only]
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
        return completed.returncode, payload, completed.stdout + completed.stderr

    def live(self, name: str) -> Path:
        return self.skills / name / "SKILL.md"

    def snapshot(self, name: str) -> Path:
        return self.index / "snapshot" / name / "SKILL.md"

    def expected_of(self, name: str) -> dict:
        payload = json.loads((self.index / "expected-terms.json").read_text(encoding="utf-8"))
        for item in payload["skills"]:
            if item["name"] == name:
                return item
        raise AssertionError("词表里没有 %s" % name)

    def kinds(self, payload: dict) -> set[str]:
        return {item["kind"] for item in payload.get("problems", [])}

    # —— 基线 ——————————————————————————————————————————————

    def test_real_live_files_pass(self) -> None:
        """词表里的每一个 + 真的 index：必须一条命令退出 0，个数随词表。"""
        completed = subprocess.run([sys.executable, str(SCRIPT), "--json"],
                                   capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"], completed.stdout)
        self.assertEqual(payload["skill_count"], len(self.names), completed.stdout)
        self.assertEqual(payload["problems"], [], completed.stdout)

    def test_copied_tree_passes(self) -> None:
        code, payload, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertEqual(payload["problems"], [], output)

    # —— 反例一：删掉一个期望词 ————————————————————————————————

    def test_missing_term_fails(self) -> None:
        kept = self.expected_of(SAMPLE)["required_terms"][0]
        dropped = self.expected_of(SAMPLE)["required_terms"][1]
        rewrite_description(self.live(SAMPLE), "写实现之前先%s，让它变绿。（这一句故意少了另一个触发词）" % kept)
        code, payload, output = self.run_check()
        self.assertEqual(code, 1, output)
        self.assertIn("missing-term", self.kinds(payload), output)
        self.assertIn("「%s」" % dropped, output, output)
        self.assertTrue(any(item["skill"] == SAMPLE for item in payload["problems"]), output)

        # 恢复成词表登记的定稿，必须重新变绿
        rewrite_description(self.live(SAMPLE), self.expected_of(SAMPLE)["approved"])
        code, payload, output = self.run_check()
        self.assertEqual(code, 0, output)

    # —— 反例二：改坏正文一行 ————————————————————————————————

    def test_body_changed_fails_with_line_number(self) -> None:
        path = self.live(BODY_SAMPLE)
        lines = path.read_bytes().decode("utf-8").splitlines(keepends=True)
        target = len(lines) // 2            # 正文中段，肯定是 description 之后的行
        self.assertGreater(target, 3, "样本太短，取不到正文行")
        original = lines[target]
        eol = "\r\n" if original.endswith("\r\n") else "\n"
        lines[target] = original.rstrip("\r\n") + " 顺手改了一句" + eol
        path.write_bytes("".join(lines).encode("utf-8"))

        code, payload, output = self.run_check()
        self.assertEqual(code, 1, output)
        self.assertIn("body-changed", self.kinds(payload), output)
        hits = [item for item in payload["problems"]
                if item["kind"] == "body-changed" and item["skill"] == BODY_SAMPLE]
        self.assertTrue(hits, output)
        self.assertEqual(hits[0]["line"], target + 1, "差异行号必须是原文件里的 1-based 行号：\n" + output)

        # 只恢复那一行（description 仍是新的），必须重新变绿
        lines[target] = original
        path.write_bytes("".join(lines).encode("utf-8"))
        code, payload, output = self.run_check()
        self.assertEqual(code, 0, output)

    # —— 反例三：description 偏离 approved（改一个字） ————————————

    def test_description_drifted_fails(self) -> None:
        approved = self.expected_of(SAMPLE)["approved"]
        tail = "！" if approved.endswith("。") else "。"
        drifted = approved[:-1] + tail          # 只动最后一个字
        self.assertNotEqual(drifted, approved, "样本最后一个字改不动，换一个字再试")
        rewrite_description(self.live(SAMPLE), drifted)

        code, payload, output = self.run_check()
        self.assertEqual(code, 1, output)
        kinds = self.kinds(payload)
        self.assertIn("description-drifted", kinds, output)
        self.assertTrue(any(item["skill"] == SAMPLE for item in payload["problems"]), output)
        self.assertNotIn("body-changed", kinds, "只改 description 不该落到正文那条上：\n" + output)

        # 贴回定稿，必须重新变绿
        rewrite_description(self.live(SAMPLE), approved)
        code, payload, output = self.run_check()
        self.assertEqual(code, 0, output)

    # —— 反例四：词表与处置表对不上 ————————————————————————————

    def test_scope_mismatch_fails(self) -> None:
        path = self.index / "expected-terms.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["skills"] = [item for item in payload["skills"] if item["name"] != "shuo-ren-hua"]
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        code, payload_out, output = self.run_check()
        self.assertEqual(code, 1, output)
        self.assertIn("scope-mismatch", self.kinds(payload_out), output)
        self.assertIn("shuo-ren-hua", output, output)

    # —— 反例五：description 过长 ————————————————————————————

    def test_too_long_fails(self) -> None:
        terms = self.expected_of(SAMPLE)["required_terms"]
        long_text = "、".join(terms) + "。" + "这一句是为了把长度顶到上限以上的填充文字。" * 12
        rewrite_description(self.live(SAMPLE), long_text)
        code, payload, output = self.run_check()
        self.assertEqual(code, 1, output)
        self.assertIn("too-long", self.kinds(payload), output)

    # —— 反例六：快照丢了 ——————————————————————————————————

    def test_missing_snapshot_fails(self) -> None:
        self.snapshot(SAMPLE).unlink()
        code, payload, output = self.run_check()
        self.assertEqual(code, 1, output)
        self.assertIn("no-snapshot", self.kinds(payload), output)

    # —— 反例七：live 文件没了 ——————————————————————————————

    def test_missing_live_file_fails(self) -> None:
        self.live(SAMPLE).unlink()
        code, payload, output = self.run_check()
        self.assertEqual(code, 1, output)
        self.assertIn("no-skill-file", self.kinds(payload), output)

    # —— AC07：按快照还原 ——————————————————————————————————

    def test_snapshot_restore_makes_body_check_green(self) -> None:
        """改一行正文 → 报 body-changed；按快照整份拷回 → 正文与 description 都回到基线，全绿。"""
        path = self.live(BODY_SAMPLE)
        lines = path.read_bytes().decode("utf-8").splitlines(keepends=True)
        lines[5] = lines[5].rstrip("\r\n") + " 被改过\n"
        path.write_bytes("".join(lines).encode("utf-8"))
        code, payload, output = self.run_check(only="body")
        self.assertEqual(code, 1, output + "（还没还原就应该是红的）")

        shutil.copyfile(self.snapshot(BODY_SAMPLE), path)
        code, payload, output = self.run_check(only="body")
        self.assertEqual(code, 0, output)
        self.assertEqual(payload["problems"], [], output)

        # 快照就是 live 的逐字节副本，还原之后全量检查也必须是绿的
        code, payload, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertEqual(payload["problems"], [], output)

    # —— 反例八：词表条目数变了，报表里的个数跟着变（不是固定 19） ———

    def test_skill_count_follows_word_list(self) -> None:
        path = self.index / "expected-terms.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        dropped = payload["skills"].pop()
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        code, payload_out, output = self.run_check()
        self.assertEqual(code, 1, output)
        self.assertEqual(payload_out["skill_count"], len(self.names) - 1, output)
        self.assertIn("scope-mismatch", self.kinds(payload_out), output)
        self.assertIn(dropped["name"], output, output)

    # —— 反例九：词表里没有 approved ————————————————————————

    def test_approved_missing_fails(self) -> None:
        path = self.index / "expected-terms.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload["skills"]:
            if item["name"] == SAMPLE:
                item.pop("approved", None)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        code, payload_out, output = self.run_check()
        self.assertEqual(code, 1, output)
        self.assertIn("approved-missing", self.kinds(payload_out), output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
