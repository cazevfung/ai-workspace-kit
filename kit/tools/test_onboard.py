"""启动层计划器的用例：步骤、参数、失败停点与零副作用。

这些断言是插件一致性检查的锚：`plugin/draft/main.js` 里的 PLANS 必须与
`onboard.build_plans()` 同源，`tools/test_plugin_plan.py` 会拿这份输出去比。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import onboard  # noqa: E402


def run_cli(*args, cwd=None):
    return subprocess.run(
        [sys.executable, str(ROOT / "onboard.py"), *args],
        cwd=str(cwd or ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def snapshot(folder: Path):
    return sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*"))


class PlanShapeTest(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="onboard-plan-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.workspace, ignore_errors=True))
        self.plans = onboard.build_plans(self.workspace, project_name="演示项目")

    def test_bootstrap_has_the_four_host_phases_plus_the_local_receipt(self):
        steps = self.plans["bootstrap"]["steps"]
        self.assertEqual(["list_projects", "create_project", "send_to_session", "list_sessions", "write_receipt"],
                         [step["tool"] for step in steps])
        self.assertEqual([1, 2, 3, 4, 5], [step["order"] for step in steps])

    def test_bootstrap_plan_has_local_receipt_step(self):
        """C2 的锚：回执是第 5 步，标成 local，路径落在 <workspace>/.data 下。"""
        steps = self.plans["bootstrap"]["steps"]
        self.assertEqual(5, len(steps))
        receipt = steps[4]
        self.assertEqual("write_receipt", receipt["tool"])
        self.assertEqual("local", receipt["kind"])
        self.assertEqual("local", receipt["server"])
        self.assertTrue(receipt["args"]["path"].endswith("/onboard-receipt.json"), receipt["args"]["path"])
        self.assertEqual(list(onboard.RECEIPT_FIELDS), receipt["args"]["fields"])
        self.assertEqual(["project_path", "target_session_id", "working_dir", "created_at"],
                         list(onboard.RECEIPT_FIELDS))

    def test_the_receipt_step_is_not_declared_as_a_host_tool(self):
        self.assertNotIn("write_receipt", onboard.CINDY_HELPER_TOOLS)
        self.assertNotIn("write_receipt", onboard.TASK_CENTER_TOOLS)

    def test_create_project_and_send_to_session_have_complete_arguments(self):
        create = self.plans["bootstrap"]["steps"][1]
        self.assertEqual(Path(self.workspace).resolve().as_posix(),
                         Path(create["args"]["working_dir"]).as_posix())
        self.assertEqual("演示项目", create["args"]["name"])
        send = self.plans["bootstrap"]["steps"][2]
        self.assertEqual(Path(self.workspace).resolve().as_posix(),
                         Path(send["args"]["project_path"]).as_posix())
        self.assertIn("AGENTS.md", send["args"]["message"])
        self.assertTrue(send["args"]["title"])
        self.assertNotIn("target_session_id", send["args"])  # create 模式才新建会话

    def test_dispatch_reuses_the_same_scheduler_step(self):
        steps = self.plans["dispatch_ticket"]["steps"]
        self.assertEqual(["get_ticket", "claim_ticket", "send_to_session", "list_sessions"],
                         [step["tool"] for step in steps])
        bootstrap_send = self.plans["bootstrap"]["steps"][2]
        dispatch_send = steps[2]
        self.assertEqual(set(bootstrap_send["args"]), set(dispatch_send["args"]))
        self.assertEqual(bootstrap_send["server"], dispatch_send["server"])
        self.assertEqual("create（不传 target_session_id 才是新建任务）", dispatch_send["mode"])

    def test_only_known_helper_tools_are_used(self):
        for plan in self.plans.values():
            for step in plan["steps"]:
                if step["server"] == "cindy_helper":
                    self.assertIn(step["tool"], onboard.CINDY_HELPER_TOOLS)
                elif step["server"] == "task_center":
                    self.assertIn(step["tool"], onboard.TASK_CENTER_TOOLS)
                else:
                    # 只有 local 步可以不走宿主；它必须自己标 kind，且不能蹭宿主工具名。
                    self.assertEqual("local", step["server"])
                    self.assertEqual("local", step.get("kind"))
                    self.assertNotIn(step["tool"], onboard.CINDY_HELPER_TOOLS)

    def test_every_step_has_a_failure_stop(self):
        for name, plan in self.plans.items():
            self.assertEqual({step["tool"] for step in plan["steps"]},
                             set(onboard.FAILURES[name]))


class CliTest(unittest.TestCase):
    def test_json_prints_both_plans(self):
        result = run_cli("--json")
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        bootstrap = payload["plans"]["bootstrap"]["steps"]
        tools = [step["tool"] for step in bootstrap]
        self.assertIn("create_project", tools)
        self.assertIn("send_to_session", tools)
        for step in bootstrap:
            self.assertTrue(step["args"])
        self.assertIn("dispatch_ticket", payload["plans"])

    def test_json_reports_five_bootstrap_steps_and_four_host_tools(self):
        result = run_cli("--json")
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        steps = payload["plans"]["bootstrap"]["steps"]
        self.assertEqual(5, len(steps))
        self.assertEqual("local", steps[4]["kind"])
        self.assertTrue(steps[4]["args"]["path"].endswith("/onboard-receipt.json"))
        self.assertEqual(["list_projects", "create_project", "send_to_session", "list_sessions"],
                         payload["cindy_helper_tools"])

    def test_dry_run_has_no_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            (workspace / "marker.txt").write_text("unchanged", encoding="utf-8")
            before = snapshot(workspace)
            result = run_cli("--workspace", str(workspace), "--dry-run")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("预演", result.stdout)
            self.assertEqual(before, snapshot(workspace))

    def test_text_output_names_each_step(self):
        result = run_cli()
        self.assertEqual(0, result.returncode, result.stderr)
        for name in ("list_projects", "create_project", "send_to_session", "list_sessions"):
            self.assertIn(name, result.stdout)
        self.assertIn("失败停点", result.stdout)

    def test_missing_workspace_is_a_usage_error(self):
        result = run_cli("--workspace", str(Path(tempfile.gettempdir()) / "does-not-exist-onboard"))
        self.assertEqual(2, result.returncode)


if __name__ == "__main__":
    unittest.main()
