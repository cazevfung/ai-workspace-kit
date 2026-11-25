"""`submissions.artifacts` 只允许 path / sha256 / size 三键：两道写边各查一次（AW-159）。

起因是 HSE-008 的提交 `1cd60777`：它的五项 artifacts 都多了一个 `role`
（`{"path": "链-设计手记/机制.md", "role": "机制.md", "sha256": ..., "size": ...}`），
全库仅此一条。`review_result` 用整份字典逐项相等来判断成果有没有变，所以那一行
永远对不上——而它报的是 `ARTIFACT_CHANGED`：「提交的成果变了，恢复后重新提交」。
验收人照着这句话去恢复、去重交，重交一次还是同一条错：那句错话不是在描述这一行的
真实问题，它把「这一行的形状不对」说成了「文件被改过」。

这张单要的是两句能指到位置的话：

- 提交侧（`submit_result`）：键集合不对时报 `ARTIFACT_KEYS` 并点名字段名——
  「artifacts[0] 多出 role」，而不是让问题留到验收时才暴露。
- 验收侧（`review_result`）：比指纹**之前**先查形状，报的是哪条提交、第几项、
  多出/缺少哪个键、期望是哪三个键——`ARTIFACT_KEYS`，不是 `ARTIFACT_CHANGED`。
- 真的改过文件时仍然是 `ARTIFACT_CHANGED`，但要说清是哪个键变了（`sha256` 还是 `size`）。

跑法：

    python -m unittest tests.test_artifact_keys -v
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from task_center import Center
from task_center.artifacts import ARTIFACT_KEYS, fingerprint as real_fingerprint

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "89abcdef0123456789abcdef0123456789abcdef"


class ArtifactKeysTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "中文项目"
        self.project.mkdir()
        (self.project / "PRD.md").write_text("需求第一版", encoding="utf-8")
        (self.project / "result.md").write_text("1. 检查步骤", encoding="utf-8")
        (self.project / "notes.md").write_text("2. 备注", encoding="utf-8")
        self.db = self.root / "center.sqlite3"
        self.center = Center(self.db)
        self.counter = 0
        self.ids = {}
        self.ok("init")
        self.ok("register_project", {"project_id": "p1", "name": "项目一", "root": str(self.project)})

    # --- 夹具 -------------------------------------------------------------

    def call(self, op, payload=None, request_id=None):
        self.counter += 1
        return self.center.execute(op, self.resolve(payload or {}), request_id or f"req-{self.counter}")

    def resolve(self, payload):
        alias = payload.get("ticket_id")
        real = self.ids.get(alias) if isinstance(alias, str) else None
        return {**payload, "ticket_id": real} if real else payload

    def ok(self, op, payload=None, request_id=None):
        result = self.call(op, payload, request_id)
        self.assertTrue(result["ok"], result)
        return result["result"]

    def error(self, code, op, payload=None, request_id=None):
        result = self.call(op, payload, request_id)
        self.assertFalse(result["ok"], result)
        self.assertEqual(code, result["error"]["code"], result)
        return result

    def scope(self, **extra):
        return {"project_id": "p1", "ticket_id": self.ids["t1"], **extra}

    def running(self, session="executor-1"):
        created = self.ok("create_ticket", {
            "project_id": "p1", "title": "整理检查清单",
            "deliverable": "一份逐项可勾选的检查清单", "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        })
        self.ids["t1"] = created["id"]
        attempt = self.ok("claim_ticket", self.scope(
            expected_revision=1, coordinator_session_id="coordinator-1"))["attempt_id"]
        self.ok("bind_session", self.scope(
            attempt_id=attempt, session_id=session, project_root=str(self.project)))
        return attempt

    def submit_payload(self, attempt, artifacts=("result.md",)):
        return self.scope(attempt_id=attempt, session_id="executor-1", artifacts=list(artifacts),
                          summary="清单已完成", evidence=["人工检查条目"], risks=[],
                          head_sha=HEAD_SHA, base_sha=BASE_SHA)

    def review_payload(self, attempt, submission_id, verdict="accept"):
        return self.scope(attempt_id=attempt, submission_id=submission_id,
                          reviewer_session_id="reviewer-1", verdict=verdict,
                          checks=[{"id": "C1", "status": "pass", "evidence": "跑了一遍，全绿"}],
                          reason="按判据核过")

    def rewrite_artifacts(self, transform):
        """绕开工具改这一行：这正是 HSE-008 那条行来的方式，也是要能诊断出来的形状。"""
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        row = conn.execute("SELECT id, artifacts FROM submissions").fetchone()
        conn.execute("UPDATE submissions SET artifacts=? WHERE id=?",
                     (json.dumps(transform(json.loads(row[1])), ensure_ascii=False), row[0]))
        conn.commit()
        return row[0]

    # --- 验收侧：多一个键要能指到位置 --------------------------------------

    def test_artifacts_with_extra_key_is_diagnosed_not_blamed_on_the_file(self):
        """C4 的正例：多一个 role，报的是键集合，不是无解的 ARTIFACT_CHANGED。"""
        attempt = self.running()
        submission_id = self.ok("submit_result", self.submit_payload(attempt, ("result.md", "notes.md")))["submission_id"]

        def add_role(items):
            return [{**item, "role": Path(item["path"]).name} for item in items]

        self.rewrite_artifacts(add_role)
        rejected = self.call("review_result", self.review_payload(attempt, submission_id))
        self.assertFalse(rejected["ok"], rejected)
        code = rejected["error"]["code"]
        # 这一句就是这张单的判据：不是 ARTIFACT_CHANGED。
        self.assertNotEqual("ARTIFACT_CHANGED", code, rejected)
        self.assertEqual("ARTIFACT_KEYS", code, rejected)
        message = rejected["error"]["message"]
        # 指得到位置：哪条提交、第几项、多出哪个键、期望是哪三个键。
        self.assertIn(submission_id, message)
        self.assertIn("artifacts[0]", message)
        self.assertIn("role", message)
        self.assertIn("path / sha256 / size", message)

    def test_artifacts_item_that_is_only_a_path_string_is_diagnosed(self):
        """HSE-007 的 `6aa4dadf` 是这种形状：artifacts 直接是一串路径字符串。

        改动之前它会落进 `saved["path"]` 的 TypeError，被兜成 `INVALID_INPUT`
        「Invalid input or inaccessible local path」——那句话说的是路径，而事实是这一行
        根本不是提交工具写出来的形状。
        """
        attempt = self.running()
        submission_id = self.ok("submit_result", self.submit_payload(attempt, ("result.md",)))["submission_id"]
        self.rewrite_artifacts(lambda items: [item["path"] for item in items])
        rejected = self.call("review_result", self.review_payload(attempt, submission_id))
        self.assertFalse(rejected["ok"], rejected)
        self.assertEqual("ARTIFACT_KEYS", rejected["error"]["code"], rejected)
        self.assertIn("str", rejected["error"]["message"])

    def test_missing_key_is_diagnosed_with_the_missing_name(self):
        """反例方向：少一个键同样报 ARTIFACT_KEYS，并点名缺的是哪个。"""
        attempt = self.running()
        submission_id = self.ok("submit_result", self.submit_payload(attempt, ("result.md",)))["submission_id"]
        self.rewrite_artifacts(lambda items: [{k: v for k, v in item.items() if k != "size"} for item in items])
        rejected = self.call("review_result", self.review_payload(attempt, submission_id))
        self.assertEqual("ARTIFACT_KEYS", rejected["error"]["code"], rejected)
        self.assertIn("size", rejected["error"]["message"])

    # --- 验收侧：真的改过文件仍是 ARTIFACT_CHANGED，但要说是哪个键 ---------

    def test_changed_file_still_reports_artifact_changed_and_names_the_field(self):
        attempt = self.running()
        submission_id = self.ok("submit_result", self.submit_payload(attempt, ("result.md",)))["submission_id"]
        (self.project / "result.md").write_text("1. 检查步骤（被改过）", encoding="utf-8")
        rejected = self.call("review_result", self.review_payload(attempt, submission_id))
        self.assertEqual("ARTIFACT_CHANGED", rejected["error"]["code"], rejected)
        message = rejected["error"]["message"]
        self.assertIn("result.md", message)
        self.assertIn("sha256", message)
        self.assertIn("size", message)

    # --- 提交侧：同一个尺子，且点名字段名 ---------------------------------

    def test_submit_side_rejects_an_extra_key_and_names_the_field(self):
        """两道写边的第一道：提交侧自己生成的指纹也必须过同一把尺。

        正常路径上永不触发（`fingerprint()` 只写三个键），所以这里替换它来证明
        「将来有人给它加一个键」时，报错发生在提交那一刻、并且点得出字段名。
        """
        attempt = self.running()

        def leaky(root, relative):
            # 只用真函数算，再给成果那一份多盖一个键：PRD 的指纹必须照旧算得对，
            # 否则先在 `_unchanged_prd` 那里报 STALE_PRD，验不到这把尺。
            value = real_fingerprint(root, relative)
            return {**value, "role": Path(relative).name} if relative.endswith("result.md") else value

        with mock.patch("task_center.core.fingerprint", side_effect=leaky):
            rejected = self.call("submit_result", self.submit_payload(attempt))
        self.assertFalse(rejected["ok"], rejected)
        self.assertEqual("ARTIFACT_KEYS", rejected["error"]["code"], rejected)
        message = rejected["error"]["message"]
        self.assertIn("artifacts[0]", message)
        self.assertIn("role", message)
        # 一行不写：这张单还没被推到 awaiting_review。
        ticket = self.ok("get_ticket", self.scope())
        self.assertEqual("running", ticket["state"])
        self.assertEqual([], ticket["submissions"])

    def test_fingerprint_still_returns_exactly_the_three_keys(self):
        """反例守一手：正常提交的落库形状就是三键，别把尺子做成谁都过不去。"""
        attempt = self.running()
        self.ok("submit_result", self.submit_payload(attempt, ("result.md", "notes.md")))
        ticket = self.ok("get_ticket", self.scope())
        self.assertEqual(1, len(ticket["submissions"]))
        self.assertEqual(2, len(ticket["submissions"][0]["artifacts"]))
        for item in ticket["submissions"][0]["artifacts"]:
            self.assertEqual(list(ARTIFACT_KEYS), sorted(item))
            self.assertEqual({"path", "sha256", "size"}, set(item))


if __name__ == "__main__":
    unittest.main()
