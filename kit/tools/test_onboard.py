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
REPO = ROOT.parent
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


class GenericPlanTest(unittest.TestCase):
    """非 Cindy 宿主：计划里不许出现宿主工具，停点必须写死在开会话那一步。"""

    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="onboard-generic-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.workspace, ignore_errors=True))
        self.plans = onboard.build_plans(self.workspace, project_name="演示项目", host="generic")
        self.plan = self.plans["generic_dispatch"]

    def test_host_default_stays_cindy(self):
        self.assertEqual({"bootstrap", "dispatch_ticket"},
                         set(onboard.build_plans(self.workspace)))
        self.assertEqual({"generic_dispatch"}, set(self.plans))

    def test_generic_plan_is_manual_without_host_tools(self):
        self.assertEqual(
            ["create_ticket", "claim_ticket", "open_session", "bind_session", "submit_result", "get_ticket"],
            [step["tool"] for step in self.plan["steps"]],
        )
        self.assertEqual([1, 2, 3, 4, 5, 6], [step["order"] for step in self.plan["steps"]])
        for step in self.plan["steps"]:
            self.assertNotEqual("cindy_helper", step["server"], step["tool"])
            self.assertTrue(step["args"], step["tool"])

    def test_generic_open_session_is_a_local_step(self):
        step = self.plan["steps"][2]
        self.assertEqual("open_session", step["tool"])
        self.assertEqual("local", step["server"])
        self.assertEqual("local", step["kind"])
        self.assertNotIn("open_session", onboard.CINDY_HELPER_TOOLS)
        self.assertNotIn("open_session", onboard.TASK_CENTER_TOOLS)

    def test_generic_bind_step_names_a_real_session_id(self):
        step = self.plan["steps"][3]
        self.assertEqual("bind_session", step["tool"])
        self.assertEqual(onboard.SESSION_ID_PLACEHOLDER, step["args"]["session_id"])
        self.assertTrue(step["requires_real_session_id"])
        self.assertIn("真实", step["check"])
        self.assertIn("不许猜", step["check"])

    def test_generic_stop_points_name_the_aw007_incident(self):
        joined = "\n".join(self.plan["stop_points"])
        self.assertIn("AW-007", joined)
        self.assertIn("list_sessions", joined)
        self.assertIn("停在第 3 步", joined)
        self.assertIn("没有宿主建会话能力时就停在第 3 步", joined)

    def test_generic_capabilities_say_what_is_missing(self):
        capabilities = self.plan["host_capabilities"]
        self.assertFalse(capabilities["create_project"])
        self.assertFalse(capabilities["send_to_session"])
        self.assertFalse(capabilities["schedule_create"])
        self.assertTrue(capabilities["files_scripts_cron"])

    def test_generic_steps_have_failure_stops(self):
        self.assertEqual({step["tool"] for step in self.plan["steps"]},
                         set(onboard.FAILURES["generic_dispatch"]))
        self.assertIn("AW-007", onboard.FAILURES["generic_dispatch"]["open_session"])


class SessionIdGuardTest(unittest.TestCase):
    def test_a_real_looking_id_passes(self):
        ok, reason = onboard.check_session_id("a31d514c-8517-4ab9-80bf-5519cc7d65cd")
        self.assertTrue(ok, reason)
        self.assertEqual("", reason)

    def test_guesses_and_placeholders_are_rejected(self):
        for value in ("", "   ", "<真实 session_id>", "{session_id}", "unknown", "placeholder",
                      "TODO", "占位", "D:/tmp/awk106", "C:\\Users\\tmp\\run", "x" * 501):
            with self.subTest(value=value):
                ok, reason = onboard.check_session_id(value)
                self.assertFalse(ok, value)
                self.assertTrue(reason, value)

    def test_slugged_directory_names_are_rejected(self):
        """AWK-106 C4：`session_D_AI` / `D_AI工作区` 这类「把路径压平」的串既没有
        分隔符也没有占位词，旧判据当成合法 id 放行了。现在只收 UUID 形状。"""
        for value in ("session_D_AI", "D_AI工作区", "d-ai-workspace-kit",
                      "session-d-ai-workspace", "d_ai_workspace_kit", "AI工作区",
                      "workspace-2026-11-24"):
            with self.subTest(value=value):
                ok, reason = onboard.check_session_id(value)
                self.assertFalse(ok, value)
                self.assertIn("UUID", reason, value)
                self.assertIn("不要猜", reason, value)

    def test_uuid_with_a_prefix_passes(self):
        for value in ("session_a31d514c-8517-4ab9-80bf-5519cc7d65cd",
                      "sess:a31d514c-8517-4ab9-80bf-5519cc7d65cd",
                      "agent-a31d514c-8517-4ab9-80bf-5519cc7d65cd"):
            with self.subTest(value=value):
                ok, reason = onboard.check_session_id(value)
                self.assertTrue(ok, reason)

    def test_a_random_but_well_formed_id_passes_the_shape_gate_only(self):
        """第四类反例：形状合法、随手写的 id 过得了闸门。所以计划里不许声称
        「已验证存在」——只能写「必须来自宿主回执或用户」。"""
        made_up = "deadbeef-0000-4000-8000-000000000000"
        ok, reason = onboard.check_session_id(made_up)
        self.assertTrue(ok, reason)
        plan = onboard.build_plans(".", host="generic", session_id=made_up)["generic_dispatch"]
        serialized = json.dumps(plan, ensure_ascii=False)
        self.assertIn("不核存在", serialized)
        for claim in ("已验证", "确认存在", "真实存在", "确实存在"):
            self.assertNotIn(claim, serialized)



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

    def test_generic_json_carries_bind_template_and_stop(self):
        result = run_cli("--host", "generic", "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("generic", payload["host"])
        self.assertEqual(["generic_dispatch"], list(payload["plans"]))
        self.assertFalse(payload["host_capabilities"]["send_to_session"])
        self.assertTrue(payload["must_not"])
        bind = [step for step in payload["plans"]["generic_dispatch"]["steps"]
                if step["tool"] == "bind_session"][0]
        self.assertEqual(onboard.SESSION_ID_PLACEHOLDER, bind["args"]["session_id"])
        self.assertIn("AW-007", "\n".join(payload["plans"]["generic_dispatch"]["stop_points"]))

    def test_generic_text_names_every_step_and_the_stop(self):
        result = run_cli("--host", "generic")
        self.assertEqual(0, result.returncode, result.stderr)
        for name in ("create_ticket", "claim_ticket", "open_session", "bind_session",
                     "submit_result", "get_ticket"):
            self.assertIn(name, result.stdout)
        self.assertIn("停在第 3 步", result.stdout)
        self.assertIn("不许拿猜的 session_id", result.stdout)

    def test_generic_rejects_a_guessed_session_id(self):
        for value in ("D:/tmp/awk106", "unknown", "<真实 session_id>"):
            with self.subTest(value=value):
                result = run_cli("--host", "generic", "--session-id", value, "--json")
                self.assertEqual(2, result.returncode, result.stdout)
                self.assertIn("不许猜", result.stderr)

    def test_generic_fills_a_real_session_id(self):
        session = "a31d514c-8517-4ab9-80bf-5519cc7d65cd"
        result = run_cli("--host", "generic", "--session-id", session, "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        bind = [step for step in payload["plans"]["generic_dispatch"]["steps"]
                if step["tool"] == "bind_session"][0]
        self.assertEqual(session, bind["args"]["session_id"])
        self.assertEqual("命令行给出", payload["session_identity"]["source"])

    def test_session_id_is_only_for_generic(self):
        result = run_cli("--session-id", "a31d514c-8517-4ab9-80bf-5519cc7d65cd", "--json")
        self.assertEqual(2, result.returncode)
        self.assertIn("generic", result.stderr)

    def test_generic_payload_drops_cindy_tools_and_cindy_stop_points(self):
        """AWK-106 C3：`--host generic` 的 payload 里不能有 `cindy_helper_tools`，
        `failure_paths` 只能有 `generic_dispatch`——否则与 `host_capabilities` 打架。"""
        result = run_cli("--host", "generic", "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertNotIn("cindy_helper_tools", payload)
        self.assertEqual(["generic_dispatch"], list(payload["failure_paths"]))
        self.assertNotIn("cindy_helper", result.stdout)
        self.assertFalse(payload["host_capabilities"]["create_project"])

    def test_generic_cli_rejects_slugged_directory_names(self):
        for value in ("session_D_AI", "D_AI工作区", "d-ai-workspace-kit"):
            with self.subTest(value=value):
                result = run_cli("--host", "generic", "--session-id", value, "--json")
                self.assertEqual(2, result.returncode, result.stdout)
                self.assertIn("UUID", result.stderr)
                self.assertIn("不要猜", result.stderr)
                self.assertNotIn("plans", result.stdout)


class ReadmeMatchesOutputTest(unittest.TestCase):
    """C3：README 贴的 generic 输出必须与真跑出来的一字不差。

    只看文本模式是不够的（它一直是干净的）：漏的是 `--json` 的 payload。所以这里
    直接跑一遍 CLI，拿真实输出拼出 README 里那两行，再在文件里找同一串。
    """

    def readme(self, name: str) -> str:
        path = REPO / name
        if not path.is_file():
            self.skipTest(f"{name} 不在：安装出来的工作区根下 README 是 kit/README.md")
        text = path.read_text(encoding="utf-8")
        if "cindy_helper_tools" not in text:
            self.skipTest(f"{name} 不是仓库根那一份，里面没有 generic 输出可核")
        return text

    def test_readme_quotes_the_real_generic_output(self):
        result = run_cli("--host", "generic", "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        quoted = repr(sorted(payload)) + "\n" + repr(sorted(payload["failure_paths"]))
        for name in ("README.md", "README.en.md"):
            self.assertIn(quoted, self.readme(name), name)


if __name__ == "__main__":
    unittest.main()
