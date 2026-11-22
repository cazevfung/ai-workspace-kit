"""开工回执校验器的用例：三类坏法各报各的，合格回执才 0。

C4 的反例是「三类都只打印一句校验失败」——那样看回执的人分不出是没写、路径写错
还是会话标识空着。所以这里除了「每类都非零退出」，还单独断言三句原因彼此不同。
通过时那句「形状通过，未验证会话存在」也钉住：脚本只校验形状，不许暗示它反查过宿主。
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPT = ROOT.parent / "verify" / "verify_receipt.py"

GOOD = {
    "project_path": "D:/work/ws",
    "target_session_id": "0f3c1a6e-1111-4222-8333-944455556666",
    "working_dir": "D:/work/ws",
    "created_at": "2026-11-22T02:00:00+00:00",
}


def run(workspace):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--workspace", str(workspace)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )


class ReceiptTest(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="receipt-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.workspace, ignore_errors=True))

    def write(self, data):
        target = self.workspace / ".data" / "onboard-receipt.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return target

    def test_missing_receipt_says_receipt_missing_and_fails(self):
        result = run(self.workspace)
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("receipt missing", result.stdout)

    def test_relative_project_path_is_reported_as_not_absolute(self):
        self.write({**GOOD, "project_path": "work/ws"})
        result = run(self.workspace)
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("project_path is not absolute", result.stdout)
        self.assertNotIn("receipt missing", result.stdout)

    def test_empty_target_session_id_is_reported_as_empty(self):
        self.write({**GOOD, "target_session_id": "   "})
        result = run(self.workspace)
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("target_session_id is empty", result.stdout)
        self.assertNotIn("project_path is not absolute", result.stdout)

    def test_relative_working_dir_is_also_rejected(self):
        self.write({**GOOD, "working_dir": "ws"})
        result = run(self.workspace)
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("working_dir is not absolute", result.stdout)

    def test_the_three_failure_kinds_do_not_share_one_sentence(self):
        missing = run(self.workspace).stdout
        self.write({**GOOD, "project_path": "work/ws"})
        relative = run(self.workspace).stdout
        self.write({**GOOD, "target_session_id": ""})
        empty = run(self.workspace).stdout
        reasons = (missing, relative, empty)
        self.assertEqual(3, len(set(reasons)), f"三类坏法印出了同一句话：{reasons}")
        for reason in reasons:
            self.assertNotIn("校验失败", reason)

    def test_unreadable_json_is_its_own_reason(self):
        target = self.workspace / ".data" / "onboard-receipt.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{不是 JSON", encoding="utf-8")
        result = run(self.workspace)
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("receipt unreadable", result.stdout)
        self.assertNotIn("receipt missing", result.stdout)

    def test_missing_field_names_the_field(self):
        data = dict(GOOD)
        data.pop("created_at")
        self.write(data)
        result = run(self.workspace)
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("created_at is missing", result.stdout)

    def test_complete_receipt_passes_and_says_shape_only(self):
        self.write(GOOD)
        result = run(self.workspace)
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("receipt ok", result.stdout)
        self.assertIn("形状通过，未验证会话存在", result.stdout)

    def test_workspace_that_is_not_a_directory_is_a_usage_error(self):
        result = run(self.workspace / "nope")
        self.assertEqual(2, result.returncode)


if __name__ == "__main__":
    unittest.main()
