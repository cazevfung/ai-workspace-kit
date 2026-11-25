#!/usr/bin/env python3
"""check_routing.py 的反例用例：四类错误必须真的失败，恢复后必须真的通过。

跑法（直接跑文件，不用 unittest discover，也不接进 tools/run_tests.py）：

    python .agents/skills/skill-routing/scripts/test_check_routing.py
    python .agents/skills/skill-routing/scripts/test_check_routing.py -v
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check_routing.py"
REAL_ROOT = SCRIPT.parent.parent
REAL_INSTALLED_ROOTS = [REAL_ROOT.parent, Path.home() / ".agents" / "skills"]

for _stream in (sys.stdout, sys.stderr):  # 控制台按 utf-8 输出，否则中文证据看不懂
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


class CheckerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.root = self.work / "skill-routing"
        shutil.copytree(REAL_ROOT, self.root)

    def run_check(self, extra_installed_roots: list[Path] | None = None) -> tuple[int, dict, str]:
        command = [sys.executable, str(SCRIPT), "--root", str(self.root), "--json"]
        for item in REAL_INSTALLED_ROOTS + list(extra_installed_roots or []):
            command += ["--installed-root", str(item)]
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
        return completed.returncode, payload, completed.stdout + completed.stderr

    def edit(self, name: str, old: str, new: str) -> None:
        path = self.root / name
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text, "用例要改的原文在 %s 里不存在：%r" % (name, old))
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def kinds(self, payload: dict) -> set[str]:
        return {item["kind"] for item in payload.get("problems", [])}

    # —— 基线 ——————————————————————————————————————————————

    def test_real_tree_passes(self) -> None:
        code, payload, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertEqual(payload["node_count"], 31, output)
        self.assertEqual(payload["problems"], [], output)

    # —— 第一类：skill 名不存在 ——————————————————————————————

    def test_unknown_skill_name_fails(self) -> None:
        self.edit("nodes.md", "`skill:project-routing`", "`skill:no-such-skill`")
        code, payload, output = self.run_check()
        self.assertEqual(code, 1, output)
        self.assertIn("unknown-skill", self.kinds(payload), output)
        self.assertIn("no-such-skill", output)
        self.assertTrue(any(item["path"].endswith("nodes.md") for item in payload["problems"]), output)

    # —— 第二类：节点缺归口 ——————————————————————————————————

    def test_missing_owner_fails(self) -> None:
        self.edit("nodes.md", "| N02 | 动手前查有没有活跃尝试 | `action:python tools/work_gate.py --list` |",
                  "| N02 | 动手前查有没有活跃尝试 |  |")
        code, payload, output = self.run_check()
        self.assertEqual(code, 1, output)
        self.assertIn("missing-owner", self.kinds(payload), output)
        self.assertIn("N02", output)
        located = [item for item in payload["problems"] if item["kind"] == "missing-owner"]
        self.assertTrue(all(item["line"] > 0 for item in located), output)

    # —— 第三类：已装 skill 未处置 ——————————————————————————————

    def test_undispositioned_skill_fails(self) -> None:
        ghost = self.work / "extra-skills" / "ghost-unrouted"
        ghost.mkdir(parents=True)
        (ghost / "SKILL.md").write_text("---\nname: ghost-unrouted\ndescription: 用例造出来的未处置 skill\n---\n",
                                        encoding="utf-8")
        code, payload, output = self.run_check([self.work / "extra-skills"])
        self.assertEqual(code, 1, output)
        self.assertIn("undispositioned-skill", self.kinds(payload), output)
        self.assertIn("ghost-unrouted", output)

    # —— 第四类：角色表引用不存在的节点 ————————————————————————

    def test_unknown_node_in_role_table_fails(self) -> None:
        self.edit("roles.md", "| N01 | 用宿主回的 `project_root` 定项目；没有回执就列候选问人 |",
                  "| N99 | 用例造出来的不存在节点 |")
        code, payload, output = self.run_check()
        self.assertEqual(code, 1, output)
        self.assertIn("unknown-node", self.kinds(payload), output)
        self.assertIn("N99", output)
        self.assertTrue(any(item["path"].endswith("roles.md") for item in payload["problems"]), output)

    # —— 恢复：改坏再改回来，退出码必须回到 0 ——————————————

    def test_failure_then_restore_returns_zero(self) -> None:
        path = self.root / "roles.md"
        original = path.read_text(encoding="utf-8")
        path.write_text(original.replace("| N01 | 用宿主回的", "| N99 | 坏行 |\n| N01 | 用宿主回的", 1),
                        encoding="utf-8")
        broken, _, broken_output = self.run_check()
        self.assertEqual(broken, 1, broken_output)
        path.write_text(original, encoding="utf-8")
        healed, payload, healed_output = self.run_check()
        self.assertEqual(healed, 0, healed_output)
        self.assertEqual(payload["problems"], [], healed_output)

    # —— 结构性检查（不属于四类，但同样必须真的会拦） ——————————

    def test_gap_covered_mismatch_fails(self) -> None:
        self.edit("gaps.md", "| N03 | 这件事要不要开工单、切多细 |", "| N03 被删了 | 占位 |")
        code, payload, output = self.run_check()
        self.assertEqual(code, 1, output)
        self.assertIn("missing-gap", self.kinds(payload), output)

    def test_uncovered_role_union_fails(self) -> None:
        text = (self.root / "roles.md").read_text(encoding="utf-8")
        kept = "\n".join(line for line in text.splitlines() if not line.startswith("| N28 |"))
        (self.root / "roles.md").write_text(kept + "\n", encoding="utf-8")
        code, payload, output = self.run_check()
        self.assertEqual(code, 1, output)
        self.assertIn("uncovered-node", self.kinds(payload), output)
        self.assertIn("N28", output)

    def test_deleting_node_31_fails(self) -> None:
        """N31（发现的问题落在哪张单）删掉之后必须当场红：节点表不齐就不能算过。"""
        text = (self.root / "nodes.md").read_text(encoding="utf-8")
        kept = "\n".join(line for line in text.splitlines() if not line.startswith("| N31 |"))
        (self.root / "nodes.md").write_text(kept + "\n", encoding="utf-8")
        code, payload, output = self.run_check()
        self.assertEqual(code, 1, output)
        self.assertIn("orphan-node", self.kinds(payload), output)
        self.assertIn("N31", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
