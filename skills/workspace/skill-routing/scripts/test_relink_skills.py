#!/usr/bin/env python3
"""relink_skills.py 的反例用例：幂等、指向别处要重指、真实目录绝不动。

跑法（直接跑文件，不接进 tools/run_tests.py —— 那一层明文不扫 .agents/）：

    python .agents/skills/skill-routing/scripts/test_relink_skills.py
    python .agents/skills/skill-routing/scripts/test_relink_skills.py -v

全程在临时目录里跑，`~/.agents/skills` 一个字节都不碰。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "relink_skills.py"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


class RelinkCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = self.root / "source-skills"
        self.live = self.root / "live-skills"
        self.live.mkdir()
        self.names = ["alpha-skill", "beta-skill"]
        for name in self.names:
            self.write_skill(self.source / name, "from source")

    def write_skill(self, path: Path, note: str) -> None:
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_text(
            "---\nname: %s\ndescription: %s\n---\n\n正文\n" % (path.name, note),
            encoding="utf-8")

    def run_relink(self, *extra: str) -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--source-root", str(self.source),
             "--live-root", str(self.live), *extra],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def run_json(self, *extra: str) -> tuple[int, dict, str]:
        code, output = self.run_relink("--json", *extra)
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:  # 出错时输出里会多出人读的提示
            payload = {"rows": []}
        return code, payload, output

    def actions(self, payload: dict) -> dict:
        return {row["name"]: row["action"] for row in payload["rows"]}

    def same(self, target: str | None, wanted: Path) -> bool:
        """Windows 上临时目录会带 8.3 短名（202108~1），字符串比不了，按真实路径比。"""
        if not target:
            return False
        return os.path.normcase(os.path.realpath(target)) == os.path.normcase(
            os.path.realpath(wanted))

    def target_of(self, name: str) -> str | None:
        try:
            raw = os.readlink(self.live / name)
        except OSError:
            return None
        return raw[4:] if raw.startswith("\\\\?\\") else raw

    def test_creates_missing_and_is_idempotent(self) -> None:
        code, payload, output = self.run_json()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.actions(payload), {name: "created" for name in self.names}, output)
        for name in self.names:
            self.assertTrue((self.live / name / "SKILL.md").is_file(), name)
            self.assertTrue(self.same(self.target_of(name), self.source / name), output)

        code, payload, output = self.run_json()
        self.assertEqual(code, 0, "第二次不该报错：\n" + output)
        self.assertEqual(self.actions(payload), {name: "unchanged" for name in self.names}, output)
        for name in self.names:
            self.assertTrue(self.same(self.target_of(name), self.source / name), output)

    def test_relinks_when_pointing_elsewhere(self) -> None:
        self.assertEqual(self.run_relink()[0], 0)
        other = self.root / "other-skills"
        for name in self.names:
            self.write_skill(other / name, "from other")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--source-root", str(other),
             "--live-root", str(self.live), "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(self.actions(payload), {name: "relinked" for name in self.names},
                         proc.stdout)
        for name in self.names:
            self.assertTrue(self.same(self.target_of(name), other / name))
            self.assertIn("from other", (self.live / name / "SKILL.md").read_text(encoding="utf-8"))

    def test_refuses_to_delete_real_directory(self) -> None:
        real = self.live / "alpha-skill"
        real.mkdir()
        keep = real / "SKILL.md"
        keep.write_text("---\nname: alpha-skill\ndescription: 本地真目录\n---\n", encoding="utf-8")
        code, output = self.run_relink()
        self.assertEqual(code, 1, "真实目录必须让它失败而不是删掉：\n" + output)
        self.assertIn("refused", output)
        self.assertTrue(keep.is_file(), "真实目录里的文件被删了")
        self.assertEqual(keep.read_text(encoding="utf-8").count("本地真目录"), 1)

    def test_dry_run_touches_nothing(self) -> None:
        code, payload, output = self.run_json("--dry-run")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.actions(payload), {name: "would-created" for name in self.names},
                         output)
        for name in self.names:
            self.assertFalse((self.live / name).exists(), "%s 被真的建出来了" % name)

    def test_names_filter_limits_scope(self) -> None:
        code, output = self.run_relink("--names", "beta-skill")
        self.assertEqual(code, 0, output)
        self.assertFalse((self.live / "alpha-skill").exists(), "只点了 beta，alpha 不该动")
        self.assertIn("beta-skill", output)

    def test_json_output_lists_targets(self) -> None:
        code, output = self.run_relink("--json")
        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertTrue(payload["ok"])
        self.assertEqual(sorted(row["name"] for row in payload["rows"]), sorted(self.names))
        for row in payload["rows"]:
            self.assertTrue(self.same(row["target"], self.source / row["name"]), row["target"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
