#!/usr/bin/env python
"""`tools/sleep_audit.py` 的用例：先钉住「什么算命中」，再验退出码三档。

为什么要分开两块：
- 解析是纯字符串判断，和日志、时间无关，所以直接打 `sleep_calls()`，一行一条对；
  这一块里放的全是**误判不能算命中**的反例（`grep -l "sleep 270"`、`echo sleep 300`、
  python heredoc 里拼出来的 `sleep 270`），它们是 2026-11-24 手扫时真出现过的 4 次误命中。
- 扫描与退出码要读日志目录，所以造临时 JSONL：一行一个 JSON，工具调用在
  `message.content` 的 `{"type":"toolCall","arguments":{"command":...}}` 块里
  （不是 `tool_use.input`，按后者写会一条都扫不到）。

时间戳一律用固定值 `NOW`，`scan()` 的用例把 `now=NOW` 传进去；只有退出码用例必须
用真实时钟，它们按「相对现在几分钟前」造数据，仍不依赖具体日期。

跑法：

    python tools/test_sleep_audit.py     # 单独跑，用例名逐条打印
    python tools/run_tests.py            # 随主线套件跑
"""
import contextlib
import datetime
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sleep_audit

# 夹具里的「现在」：固定值，避免用例随真实时间漂移。
NOW = datetime.datetime(2026, 11, 24, 5, 0, tzinfo=datetime.timezone.utc)


def iso(minutes_ago: float) -> str:
    return (NOW - datetime.timedelta(minutes=minutes_ago)).isoformat()


def message(timestamp: str, blocks: list[dict]) -> dict:
    return {
        "type": "message",
        "timestamp": timestamp,
        "message": {"role": "assistant", "content": blocks},
    }


def tool_call(command: str, name: str = "bash") -> dict:
    return {"type": "toolCall", "id": "call-1", "name": name,
            "arguments": {"command": command, "timeout": 120}}


def write_log(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


class ParseTest(unittest.TestCase):
    """命令解析：命中与非命中各钉一遍，反例是这块的主要价值。"""

    def seconds(self, command: str) -> list[float]:
        return [item["seconds"] for item in sleep_audit.sleep_calls(command)]

    def test_plain_sleep_counts(self):
        self.assertEqual(self.seconds("sleep 300"), [300.0])

    def test_threshold_is_inclusive(self):
        self.assertEqual(self.seconds("sleep 30"), [30.0])
        self.assertEqual(self.seconds("sleep 29.9"), [29.9])

    def test_short_sleep_is_parsed_but_not_a_hit(self):
        # 解析层照常认出 5 秒，过滤在 scan() 里按阈值做；两层各自可测。
        self.assertEqual(self.seconds("sleep 5"), [5.0])

    def test_tail_keeps_what_happened_after_the_sleep(self):
        calls = sleep_audit.sleep_calls(
            "cd D:/x && sleep 270; cd D:/creator-monthly-ops && ls docs | tail -2"
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["seconds"], 270.0)
        self.assertEqual(calls[0]["tail"],
                         "cd D:/creator-monthly-ops && ls docs | tail -2")

    def test_grep_string_is_not_a_hit(self):
        # 2026-11-24 手扫时被这条误命中过：sleep 只在 grep 的参数里。
        self.assertEqual(self.seconds('grep -l "sleep 270" *.jsonl'), [])
        self.assertEqual(self.seconds("grep -l 'sleep 270' *.jsonl | tail -10"), [])

    def test_echo_is_not_a_hit(self):
        self.assertEqual(self.seconds("echo sleep 300"), [])

    def test_python_heredoc_string_is_not_a_hit(self):
        command = "python - <<'PY'\nprint('sleep 270')\nPY"
        self.assertEqual(self.seconds(command), [])

    def test_sleep_in_a_comment_is_not_a_hit(self):
        self.assertEqual(self.seconds("# sleep 300"), [])

    def test_two_sleeps_in_one_command_are_two_hits(self):
        self.assertEqual(self.seconds("sleep 60; sleep 90; echo two"), [60.0, 90.0])

    def test_sleep_after_assignment_counts(self):
        self.assertEqual(self.seconds("FOO=1 sleep 45; echo x"), [45.0])

    def test_units_are_converted_to_seconds(self):
        self.assertEqual(self.seconds("sleep 1m; echo boom"), [60.0])

    def test_sleep_behind_a_wrapper_counts(self):
        self.assertEqual(self.seconds("timeout 60 sleep 300 && echo ok"), [300.0])

    def test_sleep_inside_bash_c_counts(self):
        self.assertEqual(self.seconds("bash -c 'sleep 300; echo hi'"), [300.0])

    def test_powershell_start_sleep_counts(self):
        command = 'powershell -Command "Start-Sleep -Seconds 300; Get-Location"'
        self.assertEqual(self.seconds(command), [300.0])


class FixtureCase(unittest.TestCase):
    """每个用例一个临时目录，装几份 JSONL 日志。"""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)

    def log(self, name: str, records: list[dict]) -> Path:
        path = self.root / name
        write_log(path, records)
        return path

    def scan(self, hours: float = 24):
        return sleep_audit.scan(self.root, hours=hours, now=NOW)


class ScanTest(FixtureCase):
    def test_hit_carries_session_time_seconds_and_tail(self):
        self.log(
            "2026-11-24T04-00-00-000Z_5d70b7d1-e974-4641-b7dc-1aa78be5a1a8.jsonl",
            [message(iso(10), [tool_call(
                "sleep 270; cd D:/creator-monthly-ops && ls docs | tail -2"
            )])],
        )
        result = self.scan()
        self.assertEqual(result["summary"]["hits"], 1)
        hit = result["hits"][0]
        self.assertEqual(hit["seconds"], 270.0)
        self.assertEqual(hit["seconds_display"], "270s")
        self.assertTrue(hit["session_file"].startswith("2026-11-24T04-00-00-000Z_"))
        self.assertEqual(hit["session_id"], "5d70b7d1-e974-4641-b7dc-1aa78be5a1a8")
        self.assertIn("cd D:/creator-monthly-ops", hit["tail"])
        self.assertTrue(hit["time"].endswith("Z"))

    def test_short_sleep_and_grep_string_are_not_hits(self):
        # C2 的现场：fixture 里放一条 grep 字符串与一条 sleep 5，两样都不许计入。
        self.log("short.jsonl", [
            message(iso(5), [tool_call('grep -l "sleep 270" *.jsonl')]),
            message(iso(4), [tool_call("sleep 5")]),
        ])
        result = self.scan()
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["summary"]["hits"], 0)
        # 会话一览照常记下「这条会话发过 sleep 命令」，但不计为命中。
        self.assertEqual(result["summary"]["sessions_with_sleep"], 1)
        self.assertEqual(result["sessions"][0]["hits"], 0)

    def test_outside_the_window_is_ignored(self):
        self.log("old.jsonl", [message(iso(30 * 60), [tool_call("sleep 600")])])
        self.log("new.jsonl", [message(iso(60), [tool_call("sleep 600")])])
        result = self.scan(hours=24)
        self.assertEqual([hit["session_file"] for hit in result["hits"]], ["new.jsonl"])
        self.assertEqual(result["summary"]["hits"], 1)

    def test_zero_hours_scans_everything(self):
        self.log("old.jsonl", [message(iso(30 * 24 * 60), [tool_call("sleep 600")])])
        result = self.scan(hours=0)
        self.assertEqual(result["summary"]["hits"], 1)

    def test_hits_are_sorted_newest_first(self):
        self.log("mix.jsonl", [
            message(iso(90), [tool_call("sleep 200")]),
            message(iso(10), [tool_call("sleep 300")]),
            message(iso(50), [tool_call("sleep 100")]),
        ])
        timestamps = [hit["timestamp"] for hit in self.scan()["hits"]]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))

    def test_session_roster_reports_hits_and_sleep_commands(self):
        self.log("many.jsonl", [
            message(iso(30), [tool_call("sleep 300")]),
            message(iso(20), [tool_call("sleep 400")]),
            message(iso(10), [tool_call("sleep 2")]),
        ])
        roster = self.scan()["sessions"]
        self.assertEqual(len(roster), 1)
        self.assertEqual(roster[0]["hits"], 2)
        self.assertEqual(roster[0]["sleep_commands"], 3)

    def test_malformed_lines_are_counted_not_fatal(self):
        path = self.root / "broken.jsonl"
        path.write_text(
            "{not json\n"
            + json.dumps(message(iso(5), [tool_call("sleep 300")])) + "\n",
            encoding="utf-8",
        )
        result = self.scan()
        self.assertEqual(result["malformed_lines"], 1)
        self.assertEqual(result["summary"]["hits"], 1)

    def test_non_message_records_are_skipped(self):
        self.log("session.jsonl", [
            {"type": "session", "id": "abc", "timestamp": iso(5)},
            {"type": "model_change", "timestamp": iso(5), "modelId": "x"},
        ])
        result = self.scan()
        self.assertEqual(result["sessions_scanned"], 1)
        self.assertEqual(result["summary"]["hits"], 0)

    def test_missing_directory_is_an_error(self):
        with self.assertRaises(sleep_audit.AuditError) as raised:
            sleep_audit.scan(self.root / "没有这个目录", now=NOW)
        self.assertIn("SESSIONS_DIR_NOT_FOUND", str(raised.exception))


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = sleep_audit.main(argv)
    return code, out.getvalue(), err.getvalue()


def recent(minutes_ago: float) -> str:
    """退出码用例走真实时钟：时间戳按「相对现在」造。"""
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - datetime.timedelta(minutes=minutes_ago)).isoformat()


class EntryPointTest(unittest.TestCase):
    """退出码三档（C3）：有命中 1、无命中 0、目录不存在 2。"""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)

    def test_exit_one_when_hit(self):
        write_log(self.root / "hit.jsonl",
                  [message(recent(5), [tool_call("sleep 300")])])
        code, out, err = run_main(["--sessions-dir", str(self.root)])
        self.assertEqual(code, 1)
        self.assertIn("命中：", out)
        self.assertIn("300s", out)
        self.assertTrue(out.strip().splitlines()[-1].startswith("合计："))
        self.assertEqual(err, "")

    def test_exit_zero_when_only_false_positives(self):
        write_log(self.root / "clean.jsonl", [
            message(recent(5), [tool_call('grep -l "sleep 270" *.jsonl')]),
            message(recent(4), [tool_call("sleep 5")]),
        ])
        code, out, _ = run_main(["--sessions-dir", str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn("（无命中）", out)
        # 没有命中时连会话名都不打：列出来会让人把 sleep 5 读成命中。
        self.assertNotIn("clean.jsonl", out)
        self.assertIn("命中 0 条", out.strip().splitlines()[-1])

    def test_exit_two_when_directory_missing(self):
        code, _, err = run_main(["--sessions-dir", str(self.root / "没有这个目录")])
        self.assertEqual(code, 2)
        self.assertIn("SESSIONS_DIR_NOT_FOUND", err)

    def test_exit_two_when_hours_is_negative(self):
        code, _, err = run_main(["--sessions-dir", str(self.root), "--hours", "-1"])
        self.assertEqual(code, 2)
        self.assertIn("--hours", err)

    def test_json_output_has_hits_and_summary(self):
        write_log(self.root / "hit.jsonl",
                  [message(recent(5), [tool_call("sleep 300; echo done")])])
        code, out, _ = run_main(["--sessions-dir", str(self.root), "--json"])
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["summary"]["hits"], 1)
        self.assertEqual(payload["min_seconds"], sleep_audit.MIN_SLEEP_SECONDS)
        self.assertIn("echo done", payload["hits"][0]["tail"])
        self.assertEqual(payload["sessions"][0]["session_file"], "hit.jsonl")

    def test_json_output_is_clean_when_no_hit(self):
        code, out, _ = run_main(["--sessions-dir", str(self.root), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["hits"], [])
        self.assertEqual(payload["summary"]["hits"], 0)

    def test_help_lists_sessions_dir_and_hours(self):
        code, out, _ = run_main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("--sessions-dir", out)
        self.assertIn("--hours", out)


class ThresholdTest(unittest.TestCase):
    """阈值只在模块常量里写一次，且带出处：改数只能改这一处。"""

    def test_minimum_seconds_is_thirty(self):
        self.assertEqual(sleep_audit.MIN_SLEEP_SECONDS, 30.0)

    def test_module_source_cites_the_ticket(self):
        source = Path(sleep_audit.__file__).read_text(encoding="utf-8")
        index = source.index("MIN_SLEEP_SECONDS = 30.0")
        window = source[max(0, index - 300):index]
        self.assertIn("AW-145", window)


if __name__ == "__main__":
    unittest.main(verbosity=2)
