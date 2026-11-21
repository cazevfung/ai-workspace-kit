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

    # -- `--input` 指错路与参数不合格必须分开报 -------------------------------
    #
    # 两件事的修法不同：前者要改路径，后者要改 JSON 内容。都收成 INVALID_INPUT
    # 时，路径打错的人会去里面找错，而那句通用文案什么都没说。

    def run_raw(self, *extra):
        """不经过 run_cli 的拼参，专门测「参数本身就不对」的那几条。"""
        args = [sys.executable, "-m", "task_center", "--db", str(self.db), "list_tickets", *extra]
        process = subprocess.run(args, input=None, capture_output=True, encoding="utf-8",
                                 env={**os.environ, "PYTHONUTF8": "1"}, timeout=30)
        self.assertNotIn("Traceback", process.stderr)
        return process.returncode, json.loads(process.stdout)

    def test_missing_input_file_reports_the_path(self):
        missing = self.root / "没有这个文件.json"
        code, result = self.run_raw("--input", str(missing))
        self.assertEqual(1, code)
        self.assertEqual("INPUT_UNREADABLE", result["error"]["code"])
        self.assertIn("读不到输入文件", result["error"]["message"])
        self.assertIn(str(missing), result["error"]["message"], "要指出是哪条路读不到")

    def test_directory_as_input_is_not_a_parameter_error(self):
        code, result = self.run_raw("--input", str(self.root))
        self.assertEqual(1, code)
        self.assertEqual("INPUT_UNREADABLE", result["error"]["code"])
        self.assertIn(str(self.root), result["error"]["message"])

    def prd(self):
        path = self.root / "PRD" / "PRD-001-demo.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# demo\n", encoding="utf-8")
        return "PRD/PRD-001-demo.md"

    def ticket_payload(self, deliverable):
        return {
            "project_id": "cli-demo",
            "ticket_id": "T-001",
            "title": "命令行冒烟",
            "deliverable": deliverable,
            "prd_path": self.prd(),
            "root": str(self.root),
            "criteria": [{"id": "C1", "text": "能建出来"}],
        }

    def test_long_deliverable_prints_the_core_sentence(self):
        """501 字时打的必须是核心原句（带 500），不是命令行层另写的一句话。"""
        self.run_cli("init")
        code, result = self.run_cli("create_ticket", self.ticket_payload("长" * 501), "long", use_file=True)
        self.assertEqual(1, code)
        self.assertEqual("INVALID_INPUT", result["error"]["code"])
        self.assertIn("deliverable", result["error"]["message"])
        self.assertIn("500", result["error"]["message"])

    def test_normal_create_still_exits_zero(self):
        self.run_cli("init")
        code, result = self.run_cli("create_ticket", self.ticket_payload("能跑通的一张单"), "ok", use_file=True)
        self.assertEqual(0, code)
        self.assertEqual("ready", result["result"]["state"])
        self.assertEqual("能跑通的一张单", result["result"]["deliverable"])

    def test_unknown_operation_and_absent_database_fail(self):
        code, result = self.run_cli("no_such_operation")
        self.assertNotEqual(0, code)
        self.assertEqual("INVALID_INPUT", result["error"]["code"])
        code, result = self.run_cli("list_tickets")
        self.assertNotEqual(0, code)
        self.assertEqual("DB_NOT_INITIALIZED", result["error"]["code"])
        self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
