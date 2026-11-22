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

    def run_raw(self, *extra):
        """不经过 run_cli 的拼参，专门测「参数本身就不对」的那几条。"""
        args = [sys.executable, "-m", "task_center", "--db", str(self.db), "list_tickets", *extra]
        process = subprocess.run(args, input=None, capture_output=True, encoding="utf-8",
                                 env={**os.environ, "PYTHONUTF8": "1"}, timeout=30)
        self.assertNotIn("Traceback", process.stderr)
        return process.returncode, json.loads(process.stdout)

    def prd(self):
        path = self.root / "PRD" / "PRD-001-demo.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# demo\n", encoding="utf-8")
        return "PRD/PRD-001-demo.md"

    def ticket_payload(self, deliverable):
        # 号由库里发（schema 11 起 `create_ticket` 不再收自报 `ticket_id`），
        # 所以这里也不写那一键：写了会被「号由库里发」当场拒掉。
        return {
            "project_id": "cli-demo",
            "title": "命令行冒烟",
            "deliverable": deliverable,
            "prd_path": self.prd(),
            "root": str(self.root),
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        }

    # -- `--input` 指错路与参数不合格必须分开报 -------------------------------
    #
    # 两件事的修法不同：前者要改路径，后者要改 JSON 内容。都收成 INVALID_INPUT
    # 时，路径打错的人会去里面找错，而那句通用文案什么都没说。

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
        self.assertEqual(0, code, result)
        self.assertEqual("ready", result["result"]["state"])
        self.assertEqual("能跑通的一张单", result["result"]["deliverable"])
        self.assertEqual("CD-001", result["result"]["id"], "号由库里发：cli-demo 取每段首字母得 CD，三位数")


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
