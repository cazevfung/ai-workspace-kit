#!/usr/bin/env python
"""`tools/schedule_plan.py` 的用例：参数对不对，以及**它有没有越界**。

这个工具只该做一件事：把模板填成 `schedule_create` 的 args 打印出来。所以用例分两头：

1. **产出的参数对不对**——两条调度都在、cron 各是什么、`silentWhenIdle` 是不是真、
   派单器的 `preRunHook` 指向 `next_action.py`、队列值守有没有闸门、模板里的占位符
   换成了给定的工作区路径（换不干净就不许输出半成品）。
2. **它有没有越界**——源码里不许出现建调度的调用、不许写文件、不许起子进程；
   模板里不许留本机的调度 id / 会话 id / 会话库路径 / 模型与 provider。

第 2 类不是洁癖：这个脚本的输出会被贴进**别人的**宿主，模板里混进这台机器的一个
UUID，等于把一台机器的账本发出去；而一旦脚本自己去建调度，别人跑一次就多出两条
他没同意过、还要花钱的定时任务。

跑法：

    python tools/test_schedule_plan.py    # 单独跑，用例名逐条打印
    python tools/run_tests.py             # 随主线套件跑
"""
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
if str(TOOLS.parent) not in sys.path:
    sys.path.insert(0, str(TOOLS.parent))

import schedule_plan

SCRIPT = TOOLS / "schedule_plan.py"
AUTOMATION = TOOLS.parent / "automation"
QUEUE = AUTOMATION / "queue-watch.md"
DISPATCH = AUTOMATION / "dispatcher.md"
ROOT = TOOLS.parent
# 占位符字面量故意拆成两段：install.py 会替换 .py 里的占位符，写成整串的话
# 装出来的工作区里这个常量本身就变成真实路径，两种布局就测不出区别了（AWK-104 同款）。
PLACEHOLDER_TOKEN = "{{" "WORKSPACE" "}}"
UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                  r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


def workspace_literal():
    """装出来的工作区里，模板里应该出现的那串路径（正斜杠）。"""
    return str(ROOT).replace("\\", "/")


def neutral(text):
    """把占位符与本工作区路径都换成同一个标记，剩下的就不该再有磁盘路径了。"""
    return text.replace(PLACEHOLDER_TOKEN, "<WS>").replace(workspace_literal(), "<WS>")


def uses_placeholder():
    """源码树里模板带占位符；装出来的工作区里已经被替换成真实路径。"""
    return PLACEHOLDER_TOKEN in QUEUE.read_text(encoding="utf-8")


def run_cli(*args, module=None):
    """跑一次 `main()`，返回 (退出码, stdout, stderr)。不真的起进程，快且不依赖 cwd。"""
    target = module or schedule_plan
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = target.main(list(args))
    return code, out.getvalue(), err.getvalue()


class TemplateFilesTest(unittest.TestCase):
    """两份模板得真的在，而且文件名看得出谁是谁。"""

    def test_queue_template_exists(self):
        self.assertTrue(QUEUE.is_file(), f"缺 {QUEUE}")

    def test_dispatcher_template_exists(self):
        self.assertTrue(DISPATCH.is_file(), f"缺 {DISPATCH}")

    def test_templates_are_not_empty(self):
        for path in (QUEUE, DISPATCH):
            with self.subTest(path=path.name):
                self.assertGreater(len(path.read_text(encoding="utf-8").strip()), 500)

    def test_templates_are_parameterised(self):
        """模板里的工作区路径要么是占位符（源码树），要么就是本工作区（装出来的）。

        两种布局都得成立：源码树里写死盘符，装到别处就是别人的路径；装出来之后还
        留着占位符，`schedule_plan.py` 会把一串花括号填进 prompt。
        """
        for path in (QUEUE, DISPATCH):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertTrue(
                    PLACEHOLDER_TOKEN in text or workspace_literal() in text,
                    f"{path.name} 既没有占位符，也不指向本工作区",
                )

    def test_templates_have_no_hardcoded_drive_path(self):
        """占位符之外不许再写死磁盘路径——那就是一台机器的目录。"""
        for path in (QUEUE, DISPATCH):
            with self.subTest(path=path.name):
                text = neutral(path.read_text(encoding="utf-8"))
                self.assertIsNone(re.search(r"[A-Za-z]:[/\\]", text),
                                  f"{path.name} 里有写死的盘符路径")


class NoHostBindingsTest(unittest.TestCase):
    """判据 C4：模板与输出里不留这台机器的绑定物。"""

    def test_no_uuid(self):
        for path in (QUEUE, DISPATCH):
            with self.subTest(path=path.name):
                self.assertIsNone(UUID.search(path.read_text(encoding="utf-8")),
                                  f"{path.name} 里有 36 位 id")

    def test_no_session_db_path(self):
        for path in (QUEUE, DISPATCH):
            with self.subTest(path=path.name):
                self.assertIsNone(re.search(r"cindy-[a-z0-9]+\.db", path.read_text(encoding="utf-8"),
                                            re.I),
                                  f"{path.name} 里有宿主会话库路径")

    def test_no_target_session_binding(self):
        for path in (QUEUE, DISPATCH):
            with self.subTest(path=path.name):
                self.assertNotIn("targetSessionId", path.read_text(encoding="utf-8"))

    def test_no_model_or_provider(self):
        for path in (QUEUE, DISPATCH):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIsNone(
                    re.search(r"\b(deepseek|claude-[a-z0-9]|x-ai/|chatgpt/|gpt-[0-9])", text, re.I),
                    f"{path.name} 里点名了模型或 provider")

    def test_output_has_no_bindings(self):
        schedules = schedule_plan.build("D:/tmp/ws", "Asia/Shanghai")
        blob = json.dumps(schedules, ensure_ascii=False)
        self.assertIsNone(UUID.search(blob))
        self.assertNotIn("cindy-", blob)
        self.assertNotIn("targetSessionId", blob)


class BuildTest(unittest.TestCase):
    """产出的参数：两条都在，各自该有的都有。"""

    def setUp(self):
        self.entries = schedule_plan.build("D:/tmp/ws", "Asia/Shanghai")
        self.queue, self.dispatch = self.entries

    def test_two_schedules(self):
        self.assertEqual(len(self.entries), 2)

    def test_names_differ_and_mention_workspace(self):
        self.assertNotEqual(self.queue["name"], self.dispatch["name"])
        for entry in self.entries:
            with self.subTest(entry=entry["name"]):
                self.assertIn("ws", entry["name"])

    def test_cron_expressions(self):
        self.assertEqual(self.queue["cronExpr"], "0,30 * * * *")
        self.assertEqual(self.dispatch["cronExpr"], "*/10 * * * *")

    def test_both_are_recurring_and_silent(self):
        for entry in self.entries:
            with self.subTest(entry=entry["name"]):
                self.assertTrue(entry["recurring"])
                self.assertTrue(entry["silentWhenIdle"])

    def test_workspace_is_working_dir(self):
        for entry in self.entries:
            with self.subTest(entry=entry["name"]):
                self.assertEqual(entry["workingDir"], "D:/tmp/ws")

    def test_dispatcher_hook_is_next_action(self):
        hook = self.dispatch["preRunHook"]
        self.assertIsNotNone(hook)
        self.assertIn("next_action.py", hook["command"])
        self.assertIn("D:/tmp/ws", hook["command"])
        self.assertGreater(hook["timeoutMs"], 0)

    def test_queue_watch_has_no_hook(self):
        """队列值守故意不设闸门：会话卡住这件事不在工单库里，闸门会把它关掉。"""
        self.assertIsNone(self.queue["preRunHook"])

    def test_prompt_is_substituted(self):
        for entry in self.entries:
            with self.subTest(entry=entry["name"]):
                self.assertIn("D:/tmp/ws", entry["prompt"])
                self.assertNotIn(PLACEHOLDER_TOKEN, entry["prompt"])

    def test_prompt_keeps_its_structure(self):
        self.assertIn("schedule_silence_current_run", self.queue["prompt"])
        self.assertIn("next_action.py", self.dispatch["prompt"])

    def test_only_queue(self):
        entries = schedule_plan.build("D:/tmp/ws", "Asia/Shanghai", only="queue")
        self.assertEqual([e["cronExpr"] for e in entries], ["0,30 * * * *"])

    def test_only_dispatch(self):
        entries = schedule_plan.build("D:/tmp/ws", "Asia/Shanghai", only="dispatch")
        self.assertEqual([e["cronExpr"] for e in entries], ["*/10 * * * *"])

    def test_timezone_is_passed_through(self):
        entries = schedule_plan.build("D:/tmp/ws", "UTC")
        self.assertEqual({e["timezone"] for e in entries}, {"UTC"})

    def test_backslashes_normalized(self):
        """Windows 路径写成反斜杠也认，输出统一成正斜杠（粘到别处不会坏）。"""
        entries = schedule_plan.build("D:\\tmp\\ws", "Asia/Shanghai")
        self.assertEqual(entries[0]["workingDir"], "D:/tmp/ws")
        self.assertIn("D:/tmp/ws", entries[0]["prompt"])

    def test_trailing_slash_dropped(self):
        entries = schedule_plan.build("D:/tmp/ws/", "Asia/Shanghai")
        self.assertEqual(entries[0]["workingDir"], "D:/tmp/ws")

    def test_installed_template_is_rerooted(self):
        """装出来的模板里已经是真实路径，`--workspace` 仍得能把它们改指到别处。

        不认这种布局的后果很具体：prompt 里其实是安装时那个目录，参数看着对、
        跑起来却指着别人的工作区。
        """
        temp = Path(tempfile.mkdtemp(prefix="schedplan-"))
        installed = schedule_plan.HERE.parent.as_posix()
        for name in ("queue-watch.md", "dispatcher.md"):
            (temp / name).write_text(f"工作区：{installed}\n", encoding="utf-8")
        original = schedule_plan.AUTOMATION
        schedule_plan.AUTOMATION = temp
        try:
            entries = schedule_plan.build("D:/tmp/other", "Asia/Shanghai")
        finally:
            schedule_plan.AUTOMATION = original
        for entry in entries:
            with self.subTest(entry=entry["name"]):
                self.assertIn("D:/tmp/other", entry["prompt"])
                self.assertNotIn(installed, entry["prompt"])


class ClientTest(unittest.TestCase):
    """命令行的三条分支：正常、只要一条、参数不对。"""

    def test_json_is_parseable(self):
        code, out, _ = run_cli("--workspace", "D:/tmp/ws", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["schedules"]), 2)
        for entry in payload["schedules"]:
            self.assertIn("name", entry)
            self.assertIn("cronExpr", entry)
            self.assertIn("prompt", entry)

    def test_plain_output_mentions_both(self):
        code, out, _ = run_cli("--workspace", "D:/tmp/ws")
        self.assertEqual(code, 0)
        self.assertIn("0,30 * * * *", out)
        self.assertIn("*/10 * * * *", out)
        self.assertIn("docs/无人值守派单.md", out)

    def test_relative_workspace_rejected(self):
        code, _, err = run_cli("--workspace", "relative/dir", "--json")
        self.assertEqual(code, 2)
        self.assertIn("绝对路径", err)

    def test_missing_template_is_usage_error(self):
        original = schedule_plan.AUTOMATION
        schedule_plan.AUTOMATION = Path(tempfile.mkdtemp(prefix="schedplan-"))
        try:
            code, _, err = run_cli("--workspace", "D:/tmp/ws", "--json")
        finally:
            schedule_plan.AUTOMATION = original
        self.assertEqual(code, 2)
        self.assertIn("缺模板", err)

    def test_binding_left_in_template_is_refused(self):
        """模板里混进一个会话 id 时宁可报错，也不输出带账本的参数。"""
        temp = Path(tempfile.mkdtemp(prefix="schedplan-"))
        for name in ("queue-watch.md", "dispatcher.md"):
            (temp / name).write_text(
                "绑定到 8f14e45f-ceea-4678-9e4a-6b1d3a4c5d6e，其余照旧 " + PLACEHOLDER_TOKEN,
                encoding="utf-8")
        original = schedule_plan.AUTOMATION
        schedule_plan.AUTOMATION = temp
        try:
            code, _, err = run_cli("--workspace", "D:/tmp/ws", "--json")
        finally:
            schedule_plan.AUTOMATION = original
        self.assertEqual(code, 2)
        self.assertIn("本机绑定物", err)


class DoesNotCreateSchedulesTest(unittest.TestCase):
    """判据 C5：脚本只输出参数，不建调度、不写任何宿主状态。"""

    def setUp(self):
        self.source = SCRIPT.read_text(encoding="utf-8")

    def test_no_schedule_api_calls(self):
        for banned in ("schedule_create(", "schedule_update(", "schedule_pause(",
                       "schedule_delete(", "schedule_run_now("):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, self.source)

    def test_no_subprocess_or_network(self):
        for banned in ("import subprocess", "import requests", "urllib.request",
                       "import socket", "http.client"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, self.source)

    def test_writes_no_file(self):
        """没有 open(...'w')、没有 write_text —— 它连一个临时文件都不落。"""
        self.assertNotIn("write_text(", self.source)
        self.assertIsNone(re.search(r"open\([^)]*['\"][wa]", self.source))

    def test_no_host_config_path(self):
        """不读宿主配置：没有 APPDATA、没有 Cindy 数据目录。"""
        self.assertNotIn("APPDATA", self.source)
        self.assertNotIn("Roaming", self.source)

    def test_docstring_says_it_does_not_create(self):
        self.assertIn("不建调度", schedule_plan.__doc__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
