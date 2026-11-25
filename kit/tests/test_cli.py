import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "center.sqlite3"

    def run_cli(self, operation, data=None, request_id=None, raw=None, use_file=False):
        args = [sys.executable, "-m", "task_center", "--db", str(self.db), operation]
        if request_id:
            args += ["--request-id", request_id]
        encoded = json.dumps(data or {}, ensure_ascii=False) if raw is None else raw
        if use_file:
            path = self.root / "输入.json"
            path.write_text(encoded, encoding="utf-8")
            args += ["--input", str(path)]
            stdin = None
        else:
            args += ["--input", "-"]
            stdin = encoded
        process = subprocess.run(args, input=stdin, capture_output=True, encoding="utf-8",
                                 env={**os.environ, "PYTHONUTF8": "1"}, timeout=30)
        self.assertNotIn("Traceback", process.stderr)
        return process.returncode, json.loads(process.stdout)

    def test_init_and_read_json(self):
        code, result = self.run_cli("init")
        self.assertEqual(0, code)
        self.assertTrue(result["ok"])
        code, result = self.run_cli("list_tickets")
        self.assertEqual([], result["result"]["items"])

    def test_json_file_and_unicode_roundtrip(self):
        self.run_cli("init")
        code, result = self.run_cli("register_project", {"project_id": "p1", "name": "中文项目", "root": str(self.root)}, "register", use_file=True)
        self.assertEqual(0, code)
        self.assertEqual("中文项目", result["result"]["name"])

    def test_invalid_json_is_structured_error(self):
        code, result = self.run_cli("list_tickets", raw="{invalid")
        self.assertNotEqual(0, code)
        self.assertEqual("INVALID_INPUT", result["error"]["code"])

    def test_unknown_operation_and_absent_database_fail(self):
        code, result = self.run_cli("no_such_operation")
        self.assertNotEqual(0, code)
        self.assertEqual("INVALID_INPUT", result["error"]["code"])
        code, result = self.run_cli("list_tickets")
        self.assertNotEqual(0, code)
        self.assertEqual("DB_NOT_INITIALIZED", result["error"]["code"])
        self.assertFalse(self.db.exists())


    def test_planned_flag_is_the_cli_spelling_of_the_same_view(self):
        """`list_tickets --planned` 与 payload 里的 planned: true 是同一个池子视图（R6.1）。"""
        self.run_cli("init")
        (self.root / "PRD.md").write_text("需求第一版", encoding="utf-8")
        self.run_cli("register_project", {"project_id": "p1", "name": "中文项目", "root": str(self.root)}, "register")
        created = self.run_cli("create_ticket", {
            "project_id": "p1", "state": "planned", "title": "排队中的单",
            "deliverable": "一份排进池子的清单", "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        }, "create-planned")
        self.assertEqual(0, created[0], created[1])
        ticket = created[1]["result"]["id"]
        # args 里带 --planned，payload 里什么都不写。
        args = [sys.executable, "-m", "task_center", "--db", str(self.db), "list_tickets", "--planned",
                "--input", "-"]
        process = subprocess.run(args, input="{}", capture_output=True, encoding="utf-8",
                                 env={**os.environ, "PYTHONUTF8": "1"}, timeout=30)
        self.assertNotIn("Traceback", process.stderr)
        self.assertEqual(0, process.returncode, process.stdout)
        result = json.loads(process.stdout)
        self.assertIn("claimable", result["result"]["columns"])
        self.assertIn("blocked_by", result["result"]["columns"])
        self.assertEqual([ticket], [item["id"] for item in result["result"]["items"]])
        # 同一个糖写进 payload 也是等价写法（`--planned` 这个键名）。
        code, same = self.run_cli("list_tickets", {"--planned": True})
        self.assertEqual(0, code, same)
        self.assertEqual(result["result"]["columns"], same["result"]["columns"])
        # 别的操作用这个糖是使用错误，不是悄悄忽略。
        bad = subprocess.run(
            [sys.executable, "-m", "task_center", "--db", str(self.db), "create_ticket", "--planned",
             "--request-id", "planned-on-create", "--input", "-"],
            input="{}", capture_output=True, encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1"}, timeout=30)
        self.assertNotEqual(0, bad.returncode)
        self.assertNotIn("Traceback", bad.stderr)


if __name__ == "__main__":
    unittest.main()
