#!/usr/bin/env python
"""`tools/idle_watch.py` 的用例：五个判定各有一条正例或反例，时间戳全部固定。

为什么要固定时间戳：这个工具唯一的产出是「等了多久」，拿 `now()` 造数据就等于
把用例和实现绑在一起，跑两次结果还不一样。所以夹具里先钉一个 `NOW`，把每条消息
的时间写成 `NOW - N 分钟`。

分工：库是临时 sqlite，只造 sessions / messages 两表（外加工单库的 attempts），
不为工具没读的列造列。判据只看 `scan()` 的结果与退出码，不看实现细节。

跑法：

    python tools/test_idle_watch.py          # 单独跑，用例名逐条打印
    python tools/run_tests.py                # 随主线套件跑
"""
import contextlib
import io
import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import idle_watch

ROOT = Path(__file__).resolve().parent.parent
WORKDIR = "{{WORKSPACE}}"
OTHER_WORKDIR = "%USERPROFILE%/Documents/别的项目"

# 夹具里的「现在」：固定值，避免用例随真实时间漂移。
NOW = 1795256444513


def stamp(minutes_ago: float) -> int:
    return int(NOW - minutes_ago * 60000)


def session_row(session_id, title="会话", workdir=WORKDIR,
                status="active", hidden_at=None):
    return {
        "id": session_id,
        "title": title,
        "working_dir": workdir,
        "status": status,
        "hidden_at": hidden_at,
        "created_at": stamp(600),
        "last_activity_at": stamp(1),
        "user_send_at": None,
    }


def message_row(session_id, role, minutes_ago, content=None, agent_meta=None,
                rewind_at=None, message_id=None):
    if content is None:
        content = '{"text":"好的，我来处理。"}'
    return {
        "id": message_id or f"{session_id}-{role}-{minutes_ago}",
        "session_id": session_id,
        "role": role,
        "content": content,
        "created_at": stamp(minutes_ago),
        "agent_meta": agent_meta,
        "rewind_at": rewind_at,
    }


def owner_meta():
    return json.dumps({"sourceDevice": {"deviceId": "x", "platform": "desktop"}})


def peer_meta():
    return json.dumps({"origin": {"kind": "session", "senderSessionId": "abc"}})


def text(value):
    return json.dumps({"text": value}, ensure_ascii=False)


def build_sessions_db(path, sessions=(), messages=()):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, title TEXT, working_dir TEXT, status TEXT,
            hidden_at INTEGER, created_at INTEGER, last_activity_at INTEGER,
            user_send_at INTEGER
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
            created_at INTEGER, agent_meta TEXT, rewind_at INTEGER
        );
        """
    )
    for row in sessions:
        conn.execute(
            "INSERT INTO sessions (id,title,working_dir,status,hidden_at,created_at,"
            "last_activity_at,user_send_at) VALUES (?,?,?,?,?,?,?,?)",
            (row["id"], row["title"], row["working_dir"], row["status"],
             row["hidden_at"], row["created_at"], row["last_activity_at"],
             row["user_send_at"]),
        )
    for row in messages:
        conn.execute(
            "INSERT INTO messages (id,session_id,role,content,created_at,agent_meta,rewind_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (row["id"], row["session_id"], row["role"], row["content"],
             row["created_at"], row["agent_meta"], row["rewind_at"]),
        )
    conn.commit()
    conn.close()


def build_ticket_db(path, attempts=(), tickets=(), reviews=()):
    """attempts 只造 idle_watch 读的列；tickets 给工单状态（关单判定用）。

    attempts 每项是 `(session_id, coordinator, state)`，第三项之后再带一个工单号
    就把它接到 tickets 上。不传 tickets 时不建 tickets 表——工单状态未知，工具按
    「未关单」处理（宁可多报一次）。

    `reviews` 每项是 `(reviewer_session_id, attempt_session_id)`：造一条验收记录，
    经 submissions 挂到那个执行会话的尝试上。不传就不建这两张表——老库里没有这一
    路来源，夹具默认也不带。
    """
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE attempts (id TEXT PRIMARY KEY, session_id TEXT,"
        " coordinator_session_id TEXT, state TEXT, ticket_id TEXT)"
    )
    for index, attempt in enumerate(attempts):
        session_id, coordinator, state = attempt[:3]
        ticket_id = attempt[3] if len(attempt) > 3 else None
        conn.execute(
            "INSERT INTO attempts (id,session_id,coordinator_session_id,state,ticket_id)"
            " VALUES (?,?,?,?,?)",
            (f"attempt-{index}", session_id, coordinator, state, ticket_id),
        )
    if tickets:
        conn.execute("CREATE TABLE tickets (id TEXT PRIMARY KEY, state TEXT)")
        for ticket_id, state in tickets:
            conn.execute("INSERT INTO tickets (id,state) VALUES (?,?)", (ticket_id, state))
    if reviews:
        conn.execute("CREATE TABLE submissions (id TEXT PRIMARY KEY, attempt_id TEXT)")
        conn.execute(
            "CREATE TABLE reviews (id TEXT PRIMARY KEY, submission_id TEXT,"
            " reviewer_session_id TEXT, verdict TEXT)"
        )
        attempt_by_session = {attempt[0]: f"attempt-{index}"
                             for index, attempt in enumerate(attempts)}
        for index, review in enumerate(reviews):
            reviewer_session_id, attempt_session_id = review[:2]
            submission_id = f"submission-{index}"
            conn.execute(
                "INSERT INTO submissions (id,attempt_id) VALUES (?,?)",
                (submission_id, attempt_by_session[attempt_session_id]),
            )
            conn.execute(
                "INSERT INTO reviews (id,submission_id,reviewer_session_id,verdict)"
                " VALUES (?,?,?,?)",
                (f"review-{index}", submission_id, reviewer_session_id, "accept"),
            )
    conn.commit()
    conn.close()


def scan_fixture(sessions=(), messages=(), attempts=(), workdirs=(WORKDIR,),
                 tickets=(), since_hours=24, include_history=False, reviews=()):
    """造两套临时库，跑一次 scan，返回结构化结果。"""
    import tempfile

    tempdir = tempfile.TemporaryDirectory()
    sessions_db = Path(tempdir.name) / "cindy-test.db"
    ticket_db = Path(tempdir.name) / "task-center.sqlite3"
    build_sessions_db(sessions_db, sessions, messages)
    build_ticket_db(ticket_db, attempts, tickets, reviews)
    sessions_conn = idle_watch.read_only(sessions_db)
    ticket_conn = idle_watch.read_only(ticket_db)
    try:
        result = idle_watch.scan(sessions_conn, ticket_conn, workdirs,
                                 wait_minutes=30, stalled_minutes=120, at_ms=NOW,
                                 since_hours=since_hours,
                                 include_history=include_history)
    finally:
        sessions_conn.close()
        ticket_conn.close()
        tempdir.cleanup()
    return result


def flagged_ids(result):
    return [item["session_id"] for item in result["waiting"]]


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = idle_watch.main(argv)
    return code, out.getvalue(), err.getvalue()


class IdleWatchTest(unittest.TestCase):
    """五个判定：等着的、刚问的、主人说完我没动的、单子侧、同伴消息。"""

    def test_waiting_main_task(self):
        """末条是我的问句、195 分钟没人回 → WAIT，等待时间从问句算起。"""
        sessions = [session_row("aaaaaaaa-1111-4111-8111-111111111111", title="排查分派工单")]
        messages = [
            message_row("aaaaaaaa-1111-4111-8111-111111111111", "user", 200,
                        content=text("帮我看一下分派工单的问题"), agent_meta=owner_meta()),
            message_row("aaaaaaaa-1111-4111-8111-111111111111", "assistant", 195,
                        content=text("要不要我先把 worktree 那部分单独拆出来问？")),
        ]
        result = scan_fixture(sessions, messages)

        self.assertEqual(len(result["waiting"]), 1)
        item = result["waiting"][0]
        self.assertEqual(item["kind"], idle_watch.MAIN_TASK)
        self.assertEqual(item["reason"], "assistant_question")
        self.assertAlmostEqual(item["waiting_minutes"], 195, delta=0.5)
        self.assertTrue(item["stalled"])
        line = idle_watch.human_line(item)
        self.assertTrue(line.startswith("WAIT STALLED 195min main_task "))
        self.assertIn("aaaaaaaa-1111-4111-8111-111111111111", line)

    def test_recent_question(self):
        """5 分钟前刚问的还没到阈值 → 不报，退出码 0。"""
        sessions = [session_row("bbbbbbbb-2222-4222-8222-222222222222")]
        messages = [
            message_row("bbbbbbbb-2222-4222-8222-222222222222", "assistant", 5,
                        content=text("你定吧？")),
        ]
        result = scan_fixture(sessions, messages)
        self.assertEqual(result["waiting"], [])
        self.assertEqual(result["summary"]["wait"], 0)

    def test_owner_spoke_no_reply(self):
        """末条是主人、之后我没有话 → OWNER_SPOKE_NO_REPLY，同样计入 WAIT。"""
        sessions = [session_row("cccccccc-3333-4333-8333-333333333333")]
        messages = [
            message_row("cccccccc-3333-4333-8333-333333333333", "assistant", 60,
                        content=text("已经改好了。")),
            message_row("cccccccc-3333-4333-8333-333333333333", "user", 45,
                        content=text("再补一条用例"), agent_meta=owner_meta()),
        ]
        result = scan_fixture(sessions, messages)

        self.assertEqual(len(result["waiting"]), 1)
        item = result["waiting"][0]
        self.assertEqual(item["reason"], "owner_no_reply")
        self.assertAlmostEqual(item["waiting_minutes"], 45, delta=0.5)
        self.assertTrue(idle_watch.human_line(item).endswith(
            idle_watch.OWNER_SPOKE_NO_REPLY))

    def test_bound_ticket_not_flagged(self):
        """绑在还在跑的工单上的会话不报；工单状态未知时照报，kind 是单子侧。"""
        running = "dddddddd-4444-4444-8444-444444444444"
        finished = "eeeeeeee-5555-4555-8555-555555555555"
        coordinator = "ffffffff-6666-4666-8666-666666666666"
        sessions = [
            session_row(running, title="AW-090 执行"),
            session_row(finished, title="AW-015 执行"),
            session_row(coordinator, title="协调会话"),
        ]
        messages = [
            message_row(sid, "assistant", minutes, content=text("要不要继续？"))
            for sid, minutes in ((running, 300), (finished, 300), (coordinator, 300))
        ]
        attempts = [
            (running, None, "running"),
            (finished, None, "accepted"),
            (None, coordinator, "running"),
        ]
        result = scan_fixture(sessions, messages, attempts)

        self.assertNotIn(running, flagged_ids(result))
        self.assertNotIn(coordinator, flagged_ids(result))
        self.assertIn(finished, flagged_ids(result))
        kinds = {item["session_id"]: item["kind"] for item in result["waiting"]}
        self.assertEqual(kinds[finished], idle_watch.TICKET_SIDE)
        self.assertEqual(result["summary"]["skipped_active_ticket"], 2)

    def test_peer_message_not_human(self):
        """同伴会话打进来的消息不算主人答复：末条是它，仍按我更早那句问句判悬空。"""
        session = "gggggggg-7777-4777-8777-777777777777"
        sessions = [session_row(session)]
        messages = [
            message_row(session, "user", 200, content=text("先做这个"), agent_meta=owner_meta()),
            message_row(session, "assistant", 90,
                        content=text("两个方案里你倾向哪个？")),
            message_row(session, "user", 2,
                        content=text("AW-090 已提交 / 状态 awaiting_review"),
                        agent_meta=peer_meta()),
        ]
        result = scan_fixture(sessions, messages)

        self.assertEqual(flagged_ids(result), [session])
        item = result["waiting"][0]
        self.assertEqual(item["reason"], "assistant_question")
        self.assertAlmostEqual(item["waiting_minutes"], 90, delta=0.5)

    def test_owner_meta_with_body_mention_still_owner(self):
        """顶部 sourceDevice、正文提到 agent-request 的主人消息仍算答复（不报悬空）。

        现场是 `a7e4ca66` 里主人手打的那条：子串判会把它当成同伴代发的。
        """
        session = "oooooooo-ffff-4fff-8fff-ffffffffffff"
        sessions = [session_row(session)]
        meta = json.dumps({
            "sourceDevice": {"deviceId": "x", "platform": "desktop"},
            "autoReviewUserText": {
                "plainBody": "把 origin / agent-request 的判法改一下，其余不动。",
            },
        })
        messages = [
            message_row(session, "assistant", 200, content=text("两个方案里你倾向哪个？")),
            message_row(session, "user", 10, content=text("第二个"), agent_meta=meta),
        ]
        result = scan_fixture(sessions, messages)

        self.assertEqual(result["waiting"], [])

    def test_peer_meta_with_source_device_not_owner(self):
        """顶部两个键都带的同伴消息不算主人已回：否则巡检自己的干预会藏掉悬空。

        顶部同时有 `origin` 与 `sourceDevice` 的实际数据目前没见过，这条是
        判法上的约定：两者并存时以 `origin` 为准（见 `meta_kind` 的 docstring）。
        保留它，是因为这是唯一一处「两个键都在」的回归位置。
        """
        session = "nnnnnnnn-eeee-4eee-8eee-eeeeeeeeeeee"
        sessions = [session_row(session)]
        both = json.dumps({
            "origin": {"kind": "session", "senderSessionId": "abc"},
            "sourceDevice": {"deviceId": "x", "platform": "desktop"},
        })
        messages = [
            message_row(session, "user", 200, content=text("先做这个"), agent_meta=owner_meta()),
            message_row(session, "assistant", 120,
                        content=text("两个方案里你倾向哪个？")),
            message_row(session, "user", 2,
                        content=text("巡检推送：接着做"), agent_meta=both),
        ]
        result = scan_fixture(sessions, messages)

        self.assertEqual(flagged_ids(result), [session])
        self.assertAlmostEqual(result["waiting"][0]["waiting_minutes"], 120, delta=0.5)


class QuestionDetectionTest(unittest.TestCase):
    """问句识别：末尾问号与「等你表态」的说法都算，收口陈述不算。"""

    def test_question_markers_and_marks(self):
        self.assertTrue(idle_watch.is_question("要不要继续？"))
        self.assertTrue(idle_watch.is_question("这一块你定"))
        self.assertTrue(idle_watch.is_question("回我一句就行"))
        self.assertTrue(idle_watch.is_question("这个怎么处理?"))
        self.assertFalse(idle_watch.is_question("已经改好了，提交完成。"))
        self.assertFalse(idle_watch.is_question(""))

    def test_markers_are_module_constants(self):
        """五个用例口径挂在常量上，改了常量就该被这条看见。"""
        for marker in ("要不要", "等你", "你定", "回一句", "需要你"):
            self.assertIn(marker, idle_watch.QUESTION_MARKERS)


class MetaKindPrecedenceTest(unittest.TestCase):
    """主人 vs 同伴：只看顶部键，顶部键优先于正文内容。

    核心一条是 `test_top_level_source_device_beats_body_text`：消息正文里提到
    `agent-request` 字样、顶部却只有 `sourceDevice` 的（`a7e4ca66` 里主人手打的
    那条），子串包含判会判成 peer，把主人的话当成同伴代发的。
    """

    def owner_with_body_mention(self):
        """顶部只有 sourceDevice，正文（autoReviewUserText）里出现同名键字样。"""
        return json.dumps({
            "sourceDevice": {"deviceId": "x", "platform": "desktop"},
            "autoReviewUserText": {
                "kind": "turn",
                "plainBody": "判法改成：顶部有 origin 或 agent-request 的算同伴；"
                             "以前的写法是 '\"sourceDevice\"' in raw，现在不这么判了。",
            },
        })

    def both_keys_meta(self):
        return json.dumps({
            "origin": {"kind": "session", "senderSessionId": "7ab2590d"},
            "sourceDevice": {"deviceId": "x", "platform": "desktop"},
        })

    def test_top_level_source_device_beats_body_text(self):
        """核心：顶部 sourceDevice + 正文提到 agent-request → 仍然是 owner。"""
        meta = self.owner_with_body_mention()
        self.assertIn("agent-request", meta)
        self.assertEqual(idle_watch.meta_kind(meta), "owner")

    def test_single_top_level_keys(self):
        """三种单键：只带 origin → peer，只带 agent-request → peer，只带 sourceDevice → owner。"""
        self.assertEqual(idle_watch.meta_kind(peer_meta()), "peer")
        self.assertEqual(
            idle_watch.meta_kind(json.dumps({"agent-request": {"kind": "dispatch"}})),
            "peer")
        self.assertEqual(idle_watch.meta_kind(owner_meta()), "owner")

    def test_both_top_level_keys_is_peer(self):
        """顶部两个键都有 → peer（两者并存时以 origin 为准），键的书写顺序不影响结论。"""
        self.assertEqual(idle_watch.meta_kind(self.both_keys_meta()), "peer")
        reversed_meta = json.dumps({
            "sourceDevice": {"deviceId": "x", "platform": "desktop"},
            "origin": {"kind": "session", "senderSessionId": "7ab2590d"},
        })
        self.assertEqual(idle_watch.meta_kind(reversed_meta), "peer")

    def test_empty_and_unparsable_is_unknown(self):
        """空串、`{}`、非 JSON 文本都是 unknown：老数据按「不是主人答复」处理。"""
        for empty in ("", "   ", "{}", "not json", "{broken"):
            self.assertEqual(idle_watch.meta_kind(empty), "unknown")

    def test_nested_keys_are_not_top_level(self):
        """嵌套在正文里的同名键不算。只看顶部，这是「不被正文骗」的另一面。"""
        nested = json.dumps({"autoReviewUserText": {"plainBody": "{'origin': 1}"}})
        self.assertEqual(idle_watch.meta_kind(nested), "unknown")


class ScanScopeTest(unittest.TestCase):
    """扫描面：别的项目、归档、隐藏、rewind 掉的都不算。"""

    def test_other_workdir_and_archived_and_hidden_skipped(self):
        kept = "hhhhhhhh-8888-4888-8888-888888888888"
        sessions = [
            session_row(kept),
            session_row("iiiiiiii-9999-4999-8999-999999999999", workdir=OTHER_WORKDIR),
            session_row("jjjjjjjj-aaaa-4aaa-8aaa-aaaaaaaaaaaa", status="archived"),
            session_row("kkkkkkkk-bbbb-4bbb-8bbb-bbbbbbbbbbbb", hidden_at=stamp(10)),
        ]
        messages = [message_row(kept, "assistant", 200, content=text("你说了算？"))]
        for row in sessions[1:]:
            messages.append(message_row(row["id"], "assistant", 200, content=text("你说了算？")))
        result = scan_fixture(sessions, messages)

        self.assertEqual(flagged_ids(result), [kept])
        self.assertEqual(result["scanned"], 1)

    def test_rewound_messages_ignored(self):
        """rewind_at 不为空的是被撤回的消息，不能当成末条。"""
        session = "llllllll-cccc-4ccc-8ccc-cccccccccccc"
        sessions = [session_row(session)]
        messages = [
            message_row(session, "assistant", 200, content=text("要不要继续？")),
            message_row(session, "assistant", 5, content=text("撤回的草稿？"),
                        rewind_at=stamp(4)),
        ]
        result = scan_fixture(sessions, messages)
        self.assertEqual(flagged_ids(result), [session])
        self.assertAlmostEqual(result["waiting"][0]["waiting_minutes"], 200, delta=0.5)

    def test_closed_turn_not_flagged(self):
        """末条是我但已收口（不是问句）→ OK，不报。"""
        session = "mmmmmmmm-dddd-4ddd-8ddd-dddddddddddd"
        sessions = [session_row(session)]
        messages = [
            message_row(session, "user", 300, content=text("做完它"), agent_meta=owner_meta()),
            message_row(session, "assistant", 300, content=text("已经做完了，提交在分支上。")),
        ]
        result = scan_fixture(sessions, messages)
        self.assertEqual(result["waiting"], [])


class HistoryAndWindowTest(unittest.TestCase):
    """关单历史与超窗口：默认不报、不计入退出码，但跳过条数必须打出来。

    这一组走 `main()`：退出码与末行合计是这条规则的产出，只测 `scan()` 看不出
    「全部跳过时退出码到底是不是 0」。真实 now 走的用例不用夹具的固定 `NOW`，
    而是把消息时间相对 `idle_watch.now_ms()` 往前推。
    """

    def setUp(self):
        import tempfile

        self._tempdir = tempfile.TemporaryDirectory()
        self.tempdir = Path(self._tempdir.name)
        self.sessions_db = self.tempdir / "cindy-test.db"
        self.ticket_db = self.tempdir / "task-center.sqlite3"

    def tearDown(self):
        self._tempdir.cleanup()

    def build(self, sessions, messages, attempts=(), tickets=(), reviews=()):
        for path in (self.sessions_db, self.ticket_db):
            if path.exists():
                path.unlink()
        build_sessions_db(self.sessions_db, sessions, messages)
        build_ticket_db(self.ticket_db, attempts, tickets, reviews)

    def message(self, session_id, minutes_ago, content=None):
        """相对真实 now 的消息：走 main() 的用例不能挂在夹具的固定 NOW 上。"""
        row = message_row(session_id, "assistant", 0, content=content)
        row["created_at"] = int(idle_watch.now_ms() - minutes_ago * 60000)
        return row

    def run_scan(self, *extra):
        return run_main(["--sessions-db", str(self.sessions_db),
                         "--db", str(self.ticket_db), *extra])

    def last_line(self, out):
        return [line for line in out.splitlines() if line.strip()][-1]

    def test_closed_ticket_not_reported(self):
        """done / cancelled 的单：默认不报、不计数退出码；--include-history 才列。"""
        done_session = "pppppppp-0001-4001-8001-000000000001"
        cancelled_session = "pppppppp-0002-4002-8002-000000000002"
        sessions = [
            session_row(done_session, title="AW-015 执行"),
            session_row(cancelled_session, title="AW-027 执行"),
        ]
        messages = [
            self.message(done_session, 300, text("要不要我接着改？")),
            self.message(cancelled_session, 400, text("两个方案你挑一个？")),
        ]
        attempts = [
            (done_session, None, "accepted", "AW-015"),
            (cancelled_session, None, "superseded", "AW-027"),
        ]
        tickets = [("AW-015", "done"), ("AW-027", "cancelled")]
        self.build(sessions, messages, attempts, tickets)

        code, out, _ = self.run_scan()
        self.assertEqual(code, 0)
        self.assertEqual(
            [line for line in out.splitlines() if line.startswith("WAIT ")], [])
        self.assertNotIn(done_session, out)
        self.assertNotIn(cancelled_session, out)
        tail = self.last_line(out)
        self.assertIn("WAIT 0 条", tail)
        self.assertIn("关单历史 2 条", tail)

        # 列出来也不算进退出码：这条是 C2 的反例面。
        code, out, _ = self.run_scan("--include-history")
        self.assertEqual(code, 0)
        historical = [line for line in out.splitlines()
                      if line.startswith("HISTORICAL ")]
        self.assertEqual(len(historical), 2)
        self.assertTrue(any(done_session in line for line in historical))
        for line in historical:
            self.assertIn(idle_watch.HISTORY_CLOSED, line)
        tail = self.last_line(out)
        self.assertIn("WAIT 0 条", tail)
        self.assertIn("关单历史 2 条", tail)

        code, out, _ = self.run_scan("--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["summary"]["wait"], 0)
        self.assertEqual(payload["summary"]["skipped_closed_ticket"], 2)
        self.assertEqual(payload["summary"]["historical"], 0)
        self.assertEqual(payload["waiting"], [])

    def test_live_ticket_reported(self):
        """没关的单照报（别调成永远绿）；真活着的尝试照旧跳过。"""
        open_session = "qqqqqqqq-0001-4001-8001-000000000001"
        running_session = "qqqqqqqq-0002-4002-8002-000000000002"
        sessions = [
            session_row(open_session, title="AW-090 执行"),
            session_row(running_session, title="AW-092 执行"),
        ]
        messages = [
            self.message(open_session, 300, text("要不要继续？")),
            self.message(running_session, 300, text("要不要继续？")),
        ]
        attempts = [
            (open_session, None, "accepted", "AW-090"),
            (running_session, None, "running", "AW-092"),
        ]
        tickets = [("AW-090", "running"), ("AW-092", "running")]
        self.build(sessions, messages, attempts, tickets)

        code, out, _ = self.run_scan()
        self.assertEqual(code, 1)
        wait_lines = [line for line in out.splitlines() if line.startswith("WAIT ")]
        self.assertEqual(len(wait_lines), 1)
        self.assertIn(open_session, wait_lines[0])
        self.assertIn(" ticket_side ", wait_lines[0])
        self.assertNotIn(running_session, out)
        tail = self.last_line(out)
        self.assertIn("关单历史 0 条", tail)
        self.assertIn("在跑工单跳过 1 条", tail)

    def test_stale_beyond_window_not_reported(self):
        """超窗口（默认 24 小时）的不报、不计入退出码；窗口算等待时长，不算建档时间。"""
        stale = "rrrrrrrr-0001-4001-8001-000000000001"
        fresh = "rrrrrrrr-0002-4002-8002-000000000002"
        old_session = "rrrrrrrr-0003-4003-8003-000000000003"
        old = session_row(old_session, title="三天前建的会话")
        old["created_at"] = stamp(30 * 24 * 60)  # 很久前建档，但刚刚才问
        sessions = [session_row(stale, title="26 小时前的问句"),
                    session_row(fresh, title="2 小时前的问句"),
                    old]
        messages = [
            self.message(stale, 26 * 60, text("这个要不要改？")),
            self.message(fresh, 120, text("这个要不要改？")),
            self.message(old_session, 120, text("这个要不要改？")),
        ]
        self.build(sessions, messages)

        code, out, _ = self.run_scan()
        self.assertEqual(code, 1)
        self.assertNotIn(stale, out)
        self.assertIn(fresh, out)      # 2 小时前那条不许被漏掉
        self.assertIn(old_session, out)  # 建档早但刚问的仍要报
        tail = self.last_line(out)
        self.assertIn("WAIT 2 条", tail)
        self.assertIn("超窗口 1 条", tail)

        code, out, _ = self.run_scan("--include-history")
        self.assertEqual(code, 1)
        historical = [line for line in out.splitlines()
                      if line.startswith("HISTORICAL ")]
        self.assertEqual(len(historical), 1)
        self.assertIn(stale, historical[0])
        self.assertIn(idle_watch.HISTORY_STALE, historical[0])
        self.assertIn("超窗口 1 条", self.last_line(out))

        code, out, _ = self.run_scan("--json")
        payload = json.loads(out)
        self.assertEqual(payload["summary"]["wait"], 2)
        self.assertEqual(payload["summary"]["skipped_stale"], 1)
        self.assertEqual(payload["summary"]["skipped_closed_ticket"], 0)


class ReviewerSessionTest(unittest.TestCase):
    """验收会话属于单子侧：不并进来就会被当成主任务，闸门永久红。

    验收会话两个尝试字段都不占（`session_id` 是执行会话、`coordinator_session_id`
    是协调会话），只挂在 `reviews.reviewer_session_id` 上。这里两条用例对着改：
    ①单已关 → 归入关单历史；②单未结 → 照报（不能为了消噪声一律静音）。
    """

    def setUp(self):
        import tempfile

        self._tempdir = tempfile.TemporaryDirectory()
        self.tempdir = Path(self._tempdir.name)
        self.sessions_db = self.tempdir / "cindy-test.db"
        self.ticket_db = self.tempdir / "task-center.sqlite3"

    def tearDown(self):
        self._tempdir.cleanup()

    def build(self, sessions, messages, attempts=(), tickets=(), reviews=()):
        for path in (self.sessions_db, self.ticket_db):
            if path.exists():
                path.unlink()
        build_sessions_db(self.sessions_db, sessions, messages)
        build_ticket_db(self.ticket_db, attempts, tickets, reviews)

    def message(self, session_id, minutes_ago, content=None):
        row = message_row(session_id, "assistant", 0, content=content)
        row["created_at"] = int(idle_watch.now_ms() - minutes_ago * 60000)
        return row

    def run_scan(self, *extra):
        return run_main(["--sessions-db", str(self.sessions_db),
                         "--db", str(self.ticket_db), *extra])

    def last_line(self, out):
        return [line for line in out.splitlines() if line.strip()][-1]

    def fixture(self, ticket_state, attempt_state):
        executor = "ssssssss-0001-4001-8001-000000000001"
        reviewer = "ssssssss-0002-4002-8002-000000000002"
        sessions = [
            session_row(executor, title="AW-095 执行"),
            session_row(reviewer, title="AW-095 独立验收"),
        ]
        messages = [
            self.message(executor, 200, text("已提交，等验收。")),
            self.message(reviewer, 100, text("要不要我把第三条条件再跑一遍？")),
        ]
        attempts = [(executor, None, attempt_state, "AW-095")]
        self.build(sessions, messages, attempts, [("AW-095", ticket_state)],
                   [(reviewer, executor)])
        return executor, reviewer

    def test_done_ticket_reviewer_is_history(self):
        """①单已 done：默认不报、不进退出码；--include-history 里出现且标 HISTORICAL。"""
        _, reviewer = self.fixture("done", "accepted")

        code, out, _ = self.run_scan()
        self.assertEqual(code, 0)
        self.assertNotIn(reviewer, out)
        self.assertEqual(
            [line for line in out.splitlines() if line.startswith("WAIT ")], [])
        tail = self.last_line(out)
        self.assertIn("主任务 0 条", tail)
        self.assertIn("关单历史 1 条", tail)

        code, out, _ = self.run_scan("--include-history")
        self.assertEqual(code, 0)
        historical = [line for line in out.splitlines()
                      if line.startswith("HISTORICAL ")]
        self.assertEqual(len(historical), 1)
        self.assertIn(reviewer, historical[0])
        self.assertIn(idle_watch.HISTORY_CLOSED, historical[0])
        self.assertIn(" ticket_side ", historical[0])

        code, out, _ = self.run_scan("--json")
        payload = json.loads(out)
        self.assertEqual(payload["summary"]["wait"], 0)
        self.assertEqual(payload["summary"]["main_task"], 0)
        self.assertEqual(payload["summary"]["skipped_closed_ticket"], 1)

    def test_awaiting_review_ticket_reviewer_still_reported(self):
        """②单未结（awaiting_review）：验收会话等人的那句照报，不能被吞掉。

        夹具把尝试写成 `submitted`——它在「活着的尝试」名单里。验收会话不该因此
        被当作「沉默归工单流程管」：验收会话不是执行会话，这里必须报。
        """
        executor, reviewer = self.fixture("awaiting_review", "submitted")

        code, out, _ = self.run_scan()
        self.assertEqual(code, 1)
        wait_lines = [line for line in out.splitlines() if line.startswith("WAIT ")]
        self.assertEqual(len(wait_lines), 1)
        self.assertIn(reviewer, wait_lines[0])
        self.assertIn(" ticket_side ", wait_lines[0])
        self.assertNotIn(executor, out)  # 执行会话绑在活着的尝试上，照旧跳过
        tail = self.last_line(out)
        self.assertIn("主任务 0 条", tail)
        self.assertIn("单子侧 1 条", tail)
        self.assertIn("关单历史 0 条", tail)
        self.assertIn("在跑工单跳过 1 条", tail)


class OutputAndExitCodeTest(unittest.TestCase):
    """输出形状与退出码：0 没有 WAIT，1 有 WAIT，2 库/参数有问题。"""

    def setUp(self):
        import tempfile

        self._tempdir = tempfile.TemporaryDirectory()
        self.tempdir = Path(self._tempdir.name)
        self.sessions_db = self.tempdir / "cindy-test.db"
        self.ticket_db = self.tempdir / "task-center.sqlite3"

    def tearDown(self):
        self._tempdir.cleanup()

    def write_waiting(self, minutes_ago):
        session = "nnnnnnnn-eeee-4eee-8eee-eeeeeeeeeeee"
        # 走 main() 的用例必须挂在真实 now 上：固定 NOW 与真实时间差几分钟，
        # 29 分钟的很快就变成 30 分钟。
        message = message_row(session, "assistant", 0, content=text("要不要我现在就改？"))
        message["created_at"] = int(idle_watch.now_ms() - minutes_ago * 60000)
        for path in (self.sessions_db, self.ticket_db):
            if path.exists():
                path.unlink()
        build_sessions_db(
            self.sessions_db,
            [session_row(session, title="悬空的主任务")],
            [message],
        )
        build_ticket_db(self.ticket_db)
        return session

    def test_waiting_exits_one_with_summary_last_line(self):
        session = self.write_waiting(60)
        code, out, _ = run_main(["--sessions-db", str(self.sessions_db),
                                 "--db", str(self.ticket_db)])
        self.assertEqual(code, 1)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertTrue(lines[0].startswith("WAIT "))
        self.assertIn(" main_task ", lines[0])
        self.assertIn(session, lines[0])
        self.assertIn("min ", lines[0])
        minutes = float(lines[0].split()[1].removesuffix("min"))
        self.assertGreaterEqual(minutes, 60)
        self.assertLess(minutes, 62)
        self.assertTrue(lines[-1].startswith("合计："))
        self.assertIn("WAIT 1 条", lines[-1])

    def test_thresholds_gate_and_stalled_marker(self):
        self.write_waiting(29)
        code, out, _ = run_main(["--sessions-db", str(self.sessions_db),
                                 "--db", str(self.ticket_db)])
        self.assertEqual(code, 0)
        self.assertFalse([line for line in out.splitlines() if line.startswith("WAIT ")])

        self.write_waiting(100)  # 越过 30 分钟，还没到 120
        code, out, _ = run_main(["--sessions-db", str(self.sessions_db),
                                 "--db", str(self.ticket_db)])
        self.assertEqual(code, 1)
        self.assertNotIn(idle_watch.STALLED, out)

        self.write_waiting(130)  # ≥120，另标 STALLED
        code, out, _ = run_main(["--sessions-db", str(self.sessions_db),
                                 "--db", str(self.ticket_db)])
        self.assertEqual(code, 1)
        self.assertIn(idle_watch.STALLED, out)

    def test_missing_sessions_db_exits_two(self):
        code, out, err = run_main(["--sessions-db", str(self.tempdir / "不存在.db"),
                                   "--db", str(self.ticket_db)])
        self.assertEqual(code, 2)
        self.assertIn("DB_NOT_FOUND", err)

    def test_missing_ticket_db_exits_two(self):
        self.write_waiting(60)
        code, out, err = run_main(["--sessions-db", str(self.sessions_db),
                                   "--db", str(self.tempdir / "不存在.db")])
        self.assertEqual(code, 2)
        self.assertIn("DB_NOT_FOUND", err)

    def test_json_output_is_parseable(self):
        session = self.write_waiting(60)
        code, out, _ = run_main(["--sessions-db", str(self.sessions_db),
                                 "--db", str(self.ticket_db), "--json"])
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["summary"]["wait"], 1)
        self.assertEqual(payload["waiting"][0]["session_id"], session)
        self.assertEqual(payload["waiting"][0]["kind"], idle_watch.MAIN_TASK)

    def test_default_wait_minutes_is_30(self):
        """正面钉默认值：不传 --wait-minutes，31 分钟前的问句就算 WAIT。

        旧的 29 分钟反例把默认值改成 45 或 60 依旧是绿的（45 只会让更多用例
        变红，但 29 < 45 的反例本身不红）。这一条从正面钉：默认若是 45/60，
        31 分钟就该不报，退出码会变成 0，用例当场红。
        """
        self.assertEqual(idle_watch.parse_args([]).wait_minutes, 30)
        self.write_waiting(31)
        code, out, _ = run_main(["--sessions-db", str(self.sessions_db),
                                 "--db", str(self.ticket_db)])
        self.assertEqual(code, 1)
        self.assertTrue([line for line in out.splitlines() if line.startswith("WAIT ")])

    def test_help_lists_arguments(self):
        code, out, _ = run_main(["--help"])
        self.assertEqual(code, 0)
        for flag in ("--wait-minutes", "--sessions-db", "--db", "--workdir",
                     "--since-hours", "--include-history"):
            self.assertIn(flag, out)


class ReadOnlyTest(unittest.TestCase):
    """会话库必须只读打开：可写连接会在库上动手，这条用例钉住它。"""

    def test_read_only_connection_rejects_writes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "cindy-test.db"
            build_sessions_db(path, [session_row("oooooooo-ffff-4fff-8fff-ffffffffffff")], [])
            conn = idle_watch.read_only(path)
            try:
                with self.assertRaises(sqlite3.Error):
                    conn.execute("DELETE FROM sessions")
            finally:
                conn.close()

    def test_missing_file_raises_db_not_found(self):
        with self.assertRaises(idle_watch.WatchError) as caught:
            idle_watch.read_only("C:/definitely/not/here.db")
        self.assertIn("DB_NOT_FOUND", str(caught.exception))


def _case_count():
    return unittest.TestLoader().loadTestsFromModule(sys.modules[__name__]).countTestCases()


def setUpModule():
    """把本文件的用例数单独打一行：run_tests.py 的汇总只到目录粒度。"""
    print(f"tools/test_idle_watch.py {_case_count()} 项")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    unittest.main(verbosity=2)
