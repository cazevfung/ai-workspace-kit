"""插件清单与工作区计划的一致性：两边只允许同一套步骤与失败停点。

C6 的反例是「插件写了一套与 onboard.py 不同的步骤」或「调了 cindy_helper 清单里
没有的工具名」。这两件事都不能靠人工复查——把 `plugin/draft/main.js` 里的 PLANS
与 `onboard.build_plans()` 放进同一个用例里比，改了一边忘了另一边会当场红。
"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))

import onboard  # noqa: E402

PLUGIN = REPO / "plugin" / "draft"
JAVASCRIPT = PLUGIN / "main.js"
MANIFEST = PLUGIN / "ghost.json"


def js_object(source: str, name: str) -> dict:
    match = re.search(rf"const {name} = (\{{.*?\n\}});", source, re.S)
    if not match:
        raise AssertionError(f"main.js 里找不到 const {name} = {{...}};")
    return json.loads(match.group(1))


@unittest.skipUnless(
    MANIFEST.is_file() and JAVASCRIPT.is_file(),
    "插件源码只在套件仓库里；安装出来的工作区不带 plugin/，这组用例在那里跳过",
)
class PluginPlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = JAVASCRIPT.read_text(encoding="utf-8")
        cls.plans = js_object(cls.source, "PLANS")
        cls.failures = js_object(cls.source, "FAILURES")
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cls.workspace = tempfile.mkdtemp(prefix="plugin-plan-")
        cls.reference = onboard.build_plans(cls.workspace, project_name="演示项目")

    def test_manifest_is_a_schema_v3_package(self):
        self.assertEqual(3, self.manifest["schemaVersion"])
        self.assertEqual("main.js", self.manifest["entry"])
        self.assertTrue(self.manifest["command"])
        self.assertTrue(self.manifest["minCindyVersion"])
        self.assertEqual("ai-workspace-kit", self.manifest["id"])

    def test_tools_match_the_plans(self):
        self.assertEqual({"bootstrap", "dispatch_ticket"}, set(self.plans))
        self.assertEqual({"bootstrap", "dispatch_ticket"},
                         {tool["name"] for tool in self.manifest["tools"]})

    def test_steps_are_the_same_shape_as_onboard(self):
        for name, plan in self.plans.items():
            reference = self.reference[name]["steps"]
            self.assertEqual([(step["order"], step["tool"], step["server"]) for step in reference],
                             [(step["order"], step["tool"], step["server"]) for step in plan["steps"]])
            self.assertEqual([set(step["args"]) for step in reference],
                             [set(step["args"]) for step in plan["steps"]])
            # 两边都要把回执步标成 local：一边标、一边不标同样是分叉。
            self.assertEqual([step.get("kind") for step in reference],
                             [step.get("kind") for step in plan["steps"]])

    def test_the_local_receipt_step_stays_out_of_the_host_tools(self):
        for name, plan in self.plans.items():
            local = [step for step in plan["steps"] if step.get("kind") == "local"]
            self.assertEqual(1 if name == "bootstrap" else 0, len(local),
                             f"{name} 的 local 步数不对：{[step['tool'] for step in local]}")
            for step in local:
                self.assertEqual("write_receipt", step["tool"])
                self.assertEqual("local", step["server"])
                self.assertNotIn(step["tool"], onboard.CINDY_HELPER_TOOLS)

    def test_failure_stops_match_each_step(self):
        self.assertEqual(set(self.reference), set(self.failures))
        for name, plan in self.plans.items():
            self.assertEqual({step["tool"] for step in plan["steps"]},
                             set(self.failures[name]))
            for advice in self.failures[name].values():
                self.assertTrue(advice.strip())

    def test_only_known_helper_tools_are_named(self):
        for plan in self.plans.values():
            for step in plan["steps"]:
                if step["server"] == "cindy_helper":
                    self.assertIn(step["tool"], onboard.CINDY_HELPER_TOOLS)
                elif step["server"] == "task_center":
                    self.assertIn(step["tool"], onboard.TASK_CENTER_TOOLS)
                else:
                    self.assertEqual("local", step["server"])
                    self.assertEqual("local", step.get("kind"))
                    self.assertNotIn(step["tool"], onboard.CINDY_HELPER_TOOLS)

    def test_tool_descriptions_spell_out_the_same_steps(self):
        descriptions = {tool["name"]: tool["description"] for tool in self.manifest["tools"]}
        for name, plan in self.plans.items():
            for step in plan["steps"]:
                self.assertIn(step["tool"], descriptions[name])
            self.assertIn("失败路径", descriptions[name])

    def test_locales_cover_every_declared_language(self):
        locales = self.manifest.get("locales", {})
        self.assertIn("en", locales)
        for language, relative in locales.items():
            data = json.loads((PLUGIN / relative).read_text(encoding="utf-8"))
            self.assertIn("name", data, language)
            self.assertEqual({"bootstrap", "dispatch_ticket"}, set(data["tools"]), language)


if __name__ == "__main__":
    unittest.main()
