#!/usr/bin/env python3
"""check_upstream_drift.py 的反例用例：安静的时候真安静，有变化的时候点名，但都不判失败。

跑法（直接跑文件，不接进 tools/run_tests.py —— 那一层明文不扫 .agents/）：

    python .agents/skills/skill-routing/scripts/test_upstream_drift.py
    python .agents/skills/skill-routing/scripts/test_upstream_drift.py -v

全程在临时目录里跑，不碰真的 `D:\\agent-skills\\installed` 与仓库基线。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check_upstream_drift.py"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DriftCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.upstream = self.root / "upstream"
        self.skills = self.root / "repo-skills"
        self.names = ["alpha-skill", "beta-skill"]
        for name in self.names:
            self.write(self.upstream / name / "SKILL.md", "上游原文 " + name)
            self.write(self.skills / name / "SKILL.md", "上游原文 " + name)
        self.baseline = self.root / "baseline.json"
        self.write_baseline()

    def write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def write_baseline(self) -> None:
        doc = {"recorded_at": "2026-11-17T11:09:27Z", "upstream_root": str(self.upstream),
               "skills": {}}
        for name in self.names:
            files = [{"path": p.relative_to(self.upstream / name).as_posix(),
                      "sha256": sha256_of(p)}
                     for p in sorted((self.upstream / name).rglob("*")) if p.is_file()]
            doc["skills"][name] = {"files": files}
        self.baseline.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    def run_check(self, *extra: str) -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--baseline", str(self.baseline),
             "--skills-root", str(self.skills), *extra],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def test_quiet_when_upstream_unchanged(self) -> None:
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertIn("没有需要处理的变化", output)
        self.assertNotIn("[漂移]", output)

    def test_reports_changed_file_without_failing(self) -> None:
        self.write(self.upstream / "alpha-skill" / "SKILL.md", "上游改了")
        code, output = self.run_check()
        self.assertEqual(code, 0, "上游变了不是我们的错，退出码必须是 0：\n" + output)
        self.assertIn("[漂移] alpha-skill", output)
        self.assertIn("内容变了 1 个：SKILL.md", output)
        self.assertNotIn("[漂移] beta-skill", output)

    def test_reports_added_and_removed_files(self) -> None:
        self.write(self.upstream / "alpha-skill" / "notes.md", "新增")
        (self.upstream / "beta-skill" / "SKILL.md").unlink()
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertIn("多了文件 1 个：notes.md", output)
        self.assertIn("少了文件 1 个：SKILL.md", output)

    def test_missing_upstream_directory_is_named(self) -> None:
        for path in sorted((self.upstream / "beta-skill").rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        (self.upstream / "beta-skill").rmdir()
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertIn("[漂移] beta-skill", output)
        self.assertIn("上游目录不存在", output)

    def test_reports_local_copy_divergence(self) -> None:
        self.write(self.skills / "beta-skill" / "SKILL.md", "我们本地改过")
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertNotIn("[漂移]", output, "本地改过不等于上游变了")
        self.assertIn("[副本] beta-skill 仓库副本 differs-from-baseline", output)

    def test_json_lists_drifted_names(self) -> None:
        self.write(self.upstream / "alpha-skill" / "SKILL.md", "上游改了")
        code, output = self.run_check("--json")
        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertEqual(payload["drifted"], ["alpha-skill"])

    def test_missing_baseline_is_usage_error(self) -> None:
        self.baseline.unlink()
        code, output = self.run_check()
        self.assertEqual(code, 2, output)
        self.assertIn("找不到基线", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
