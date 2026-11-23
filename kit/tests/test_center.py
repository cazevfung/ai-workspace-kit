import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from task_center import Center
from task_center.db import SCHEMA_VERSION

# 本文件里每一次提交用的 head sha。真值由 git 给（`git rev-parse HEAD`），
# 这里只要是一个合法的 40 位十六进制串：用例验的是「记下来、读得回、比得上」，
# 不是 sha 本身对不对。
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
# 这次提交是从哪个 commit 分出来的（`git merge-base HEAD master` 的输出）。
BASE_SHA = "89abcdef0123456789abcdef0123456789abcdef"


class CenterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "中文项目"
        self.project.mkdir()
        (self.project / "PRD.md").write_text("需求第一版", encoding="utf-8")
        self.db = self.root / "center.sqlite3"
        self.center = Center(self.db)
        self.counter = 0
        # 库发号之后，测试里仍用 t1 / t2 这类短名指代工单；resolve() 在发请求前换成库发的号。
        self.ids = {}
        self.ok("init")
        self.ok("register_project", {"project_id": "p1", "name": "项目一", "root": str(self.project)})

    def call(self, op, payload=None, request_id=None):
        self.counter += 1
        return self.center.execute(op, self.resolve(payload or {}), request_id or f"req-{self.counter}")

    def resolve(self, payload):
        """把 payload 里的短名换成库发的工单号。

        别名只活在测试里：`create_ticket` 不再收自报号，所以「哪张单是 t1」只能取自回执。
        只替换值、不删字段——`create_ticket` 带着 ticket_id 仍然要被拒。
        """
        alias = payload.get("ticket_id")
        real = self.ids.get(alias) if isinstance(alias, str) else None
        return {**payload, "ticket_id": real} if real else payload

    def tid(self, alias="t1"):
        return self.ids[alias]

    def ok(self, op, payload=None, request_id=None):
        result = self.call(op, payload, request_id)
        self.assertTrue(result["ok"], result)
        return result["result"]

    def error(self, code, op, payload=None, request_id=None):
        result = self.call(op, payload, request_id)
        self.assertFalse(result["ok"], result)
        self.assertEqual(code, result["error"]["code"], result)

    def ticket(self, alias="t1"):
        created = self.ok("create_ticket", {
            "project_id": "p1", "title": "整理检查清单",
            "deliverable": "一份逐项可勾选的检查清单",
            "prd_path": "PRD.md", "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        })
        self.ids[alias] = created["id"]
        return created

    def test_create_ticket_requires_deliverable(self):
        base = {"project_id": "p1", "title": "整理检查清单",
                "prd_path": "PRD.md", "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}]}
        for bad in (None, "", "   "):
            payload = dict(base) if bad is None else {**base, "deliverable": bad}
            self.error("INVALID_INPUT", "create_ticket", payload)

    def test_deliverable_is_recorded_and_revisable(self):
        created = self.ticket()
        self.assertEqual("一份逐项可勾选的检查清单", created["deliverable"])
        self.assertEqual("一份逐项可勾选的检查清单", self.ok("get_ticket", {"ticket_id": "t1", "project_id": "p1"})["deliverable"])
        revised = self.ok("revise_ticket", {
            "ticket_id": "t1", "project_id": "p1", "expected_revision": 1,
            "reason": "交付物形状变了", "deliverable": "一份带负责人栏的检查清单",
            "prd_path": "PRD.md", "criteria": [{"id": "C1", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
        })
        self.assertEqual("一份带负责人栏的检查清单", revised["deliverable"])
        self.assertEqual("一份带负责人栏的检查清单", self.ok("get_ticket", {"ticket_id": "t1", "project_id": "p1"})["deliverable"])

    def test_revision_keeps_deliverable_when_omitted(self):
        self.ticket()
        revised = self.ok("revise_ticket", {
            "ticket_id": "t1", "project_id": "p1", "expected_revision": 1,
            "reason": "只改验收条件", "prd_path": "PRD.md",
            "criteria": [{"id": "C1", "text": "含 `git diff --stat` 且退出码 0"}],
        })
        self.assertEqual("一份逐项可勾选的检查清单", revised["deliverable"])

    def test_create_and_query_unicode_project(self):
        created = self.ticket()
        self.assertEqual("ready", created["state"])
        self.assertEqual(1, created["revision"])
        # R2.1：建单那一刻只记 prd_path，指纹由 claim 取，所以这里必须是 None。
        self.assertIsNone(created["prd_sha256"])
        result = self.ok("get_ticket", {"ticket_id": "t1", "project_id": "p1"})
        self.assertEqual("整理检查清单", result["title"])
        self.assertEqual([], result["attempts"])

    def test_missing_database_is_not_created_by_query(self):
        absent = self.root / "absent.sqlite3"
        result = Center(absent).execute("list_tickets", {})
        self.assertEqual("DB_NOT_INITIALIZED", result["error"]["code"])
        self.assertFalse(absent.exists())

    def test_reinitialization_preserves_data(self):
        self.ticket()
        self.ok("init")
        self.assertEqual(1, len(self.ok("list_tickets")["items"]))

    def test_duplicate_project_root_reuses_the_registration(self):
        # root 是身份：同一路径第二次登记返回原记录，不报 CONFLICT，也不改已有标签。
        again = self.ok("register_project", {
            "project_id": "p2", "name": "重复", "root": str(self.project / "."),
        })
        self.assertEqual("p1", again["project_id"])
        self.assertEqual("项目一", again["name"])
        self.assertTrue(again["reused"])
        self.assertEqual(["p1"], [item["project_id"] for item in self.ok("list_projects")["items"]])

    def test_cross_project_query_rejected(self):
        self.ticket()
        self.error("PROJECT_MISMATCH", "get_ticket", {"ticket_id": "t1", "project_id": "p2"})

    def test_project_must_exist(self):
        self.error("NOT_FOUND", "create_ticket", {
            "project_id": "unknown", "title": "任务",
            "deliverable": "一份清单",
            "prd_path": "PRD.md", "criteria": [{"id": "C1", "text": "含 `python -m task_center --help` 且退出码 0"}],
        })

    def test_prd_path_cannot_escape_project(self):
        (self.root / "outside.md").write_text("outside")
        self.error("PATH_OUTSIDE_PROJECT", "create_ticket", {
            "project_id": "p1", "title": "任务",
            "deliverable": "一份清单",
            "prd_path": "../outside.md", "criteria": [{"id": "C1", "text": "含 `python -m task_center --help` 且退出码 0"}],
        })

    def test_nonempty_unique_criteria_required(self):
        for criteria in [[], [{"id": "C1", "text": ""}], [{"id": "C1", "text": "a"}] * 2]:
            self.error("INVALID_INPUT", "create_ticket", {
                "project_id": "p1", "title": "任务",
                "deliverable": "一份清单",
                "prd_path": "PRD.md", "criteria": criteria,
            })

    def test_exact_retry_returns_original_receipt(self):
        payload = {"project_id": "p1", "title": "任务",
                   "deliverable": "一份清单",
                   "prd_path": "PRD.md", "criteria": [{"id": "C1", "text": "含 `python -m task_center --help` 且退出码 0"}]}
        first = self.ok("create_ticket", payload, "same")
        self.assertEqual(first, self.ok("create_ticket", payload, "same"))
        self.assertEqual(1, len(self.ok("list_tickets")["items"]))
        self.assertEqual(1, len(self.ok("events", {"ticket_id": first["id"], "project_id": "p1"})["items"]))
        self.error("REQUEST_CONFLICT", "create_ticket", {**payload, "title": "不同"}, "same")

    def test_write_requires_request_id_and_rejects_extra_fields(self):
        result = self.center.execute("register_project", {"project_id": "p2", "name": "a", "root": str(self.project)})
        self.assertEqual("INVALID_INPUT", result["error"]["code"])
        self.error("INVALID_INPUT", "list_tickets", {"sql": "DELETE FROM tickets"})

    def test_list_filters_and_bounds_pagination(self):
        self.ticket("t1")
        self.ticket("t2")
        self.assertEqual(1, len(self.ok("list_tickets", {"limit": 1, "offset": 1})["items"]))
        self.assertEqual([], self.ok("list_tickets", {"project_id": "none"})["items"])
        for bad in [0, 101, True, "5"]:
            self.error("INVALID_INPUT", "list_tickets", {"limit": bad})

    def scope(self, **extra):
        return {"project_id": "p1", "ticket_id": "t1", **extra}

    def claim_payload(self, coordinator="coordinator-1", **extra):
        # 派发者与被派发者是两个会话；默认值故意与 running() 的执行会话不同。
        return self.scope(coordinator_session_id=coordinator, **extra)

    def running(self, session="executor-1"):
        claim = self.ok("claim_ticket", self.claim_payload(expected_revision=1))
        self.ok("bind_session", self.scope(attempt_id=claim["attempt_id"], session_id=session,
                                          project_root=str(self.project)))
        return claim["attempt_id"]

    def result_payload(self, attempt, session="executor-1", head_sha=HEAD_SHA, base_sha=BASE_SHA):
        (self.project / "result.md").write_text("1. 检查步骤", encoding="utf-8")
        return self.scope(attempt_id=attempt, session_id=session, artifacts=["result.md"],
                          summary="清单已完成", evidence=["人工检查条目"], risks=[],
                          head_sha=head_sha, base_sha=base_sha)

    def test_claim_requires_coordinator_session_id(self):
        self.ticket()
        for bad in (None, "", "   ", 7, "x" * 501):
            payload = self.scope(expected_revision=1)
            if bad is not None:
                payload["coordinator_session_id"] = bad
            self.error("INVALID_INPUT", "claim_ticket", payload)
        # 缺字段本身也要拒绝，不能靠值为空来冒名顶替。
        self.error("INVALID_INPUT", "claim_ticket", self.scope(expected_revision=1))
        self.assertEqual("ready", self.ok("get_ticket", self.scope())["state"])

    def test_coordinator_cannot_bind_the_attempt_it_dispatched(self):
        self.ticket()
        claim = self.claim_payload(expected_revision=1)
        attempt = self.ok("claim_ticket", claim)["attempt_id"]
        binding = self.scope(attempt_id=attempt, session_id=claim["coordinator_session_id"],
                             project_root=str(self.project))
        self.error("SELF_DISPATCH", "bind_session", binding)
        ticket = self.ok("get_ticket", self.scope())
        self.assertEqual("dispatching", ticket["state"])
        self.assertIsNone(ticket["attempts"][0]["session_id"])
        self.assertEqual(claim["coordinator_session_id"], ticket["attempts"][0]["coordinator_session_id"])
        # 换一个会话就能绑定，回执里同时看得出谁派的、谁做的。
        receipt = self.ok("bind_session", {**binding, "session_id": "executor-2"})
        self.assertEqual("executor-2", receipt["session_id"])
        self.assertEqual("coordinator-1", receipt["coordinator_session_id"])

    def test_claim_persists_before_session_binding(self):
        self.ticket()
        claim = self.ok("claim_ticket", self.claim_payload(expected_revision=1))
        ticket = self.ok("get_ticket", self.scope())
        self.assertEqual("dispatching", ticket["state"])
        self.assertEqual(claim["attempt_id"], ticket["current_attempt"])
        self.assertIsNone(ticket["attempts"][0]["session_id"])
        self.error("INVALID_STATE", "claim_ticket", self.claim_payload(expected_revision=1))

    def test_two_processes_cannot_claim_twice(self):
        self.ticket()
        code = "import json,sys; from task_center import Center; print(json.dumps(Center(sys.argv[1]).execute('claim_ticket',json.loads(sys.argv[2]),sys.argv[3])))"
        args = [sys.executable, "-c", code, str(self.db),
                json.dumps(self.resolve(self.claim_payload(expected_revision=1)))]
        processes = [subprocess.Popen(args + [f"parallel-{i}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(2)]
        results = []
        for proc in processes:
            stdout, stderr = proc.communicate(timeout=30)
            self.assertEqual(0, proc.returncode, stderr)
            results.append(json.loads(stdout))
        self.assertEqual(1, sum(item["ok"] for item in results), results)
        self.assertEqual(1, len(self.ok("get_ticket", self.scope())["attempts"]))
        # 三行：建单、赢的那次领取，以及输的那次被拒后的留痕（PRD-013 R3）。
        self.assertEqual(3, len(self.ok("events", self.scope())["items"]))

    def test_binding_wrong_project_directory_rolls_back(self):
        self.ticket()
        claim = self.ok("claim_ticket", self.claim_payload(expected_revision=1))
        self.error("PROJECT_MISMATCH", "bind_session", self.scope(attempt_id=claim["attempt_id"],
                   session_id="executor", project_root=str(self.root)))
        self.assertEqual("dispatching", self.ok("get_ticket", self.scope())["state"])

    def test_session_cannot_run_two_active_tickets(self):
        self.ticket()
        self.running()
        self.ticket("t2")
        # 这里验的是会话唯一性，不是项目串行；显式越过串行（AW-061）。
        claim = self.ok("claim_ticket", self.claim_payload(ticket_id="t2", expected_revision=1,
                                                         allow_parallel=True,
                                                         parallel_reason="验的是会话唯一性，不是项目串行"))
        self.error("CONFLICT", "bind_session", self.scope(ticket_id="t2", attempt_id=claim["attempt_id"],
                   session_id="executor-1", project_root=str(self.project)))
        self.assertEqual("dispatching", self.ok("get_ticket", self.scope(ticket_id="t2"))["state"])

    def test_submit_requires_a_full_head_sha(self):
        """C3：没有 40 位十六进制的 head_sha 就交不了成果。

        这条字段存在的理由只有一个：验收与合并之间隔着一次「再提交一次」的机会，
        库里不记被验收的那棵树，事后就分不出合进去的到底是不是它。所以缺字段、
        长度不对、不是十六进制，一律 INVALID_INPUT，而不是存一个近似值。
        """
        self.ticket()
        attempt = self.running()
        payload = self.result_payload(attempt)
        cases = [None, "", "   ", HEAD_SHA[:39], HEAD_SHA + "0", "Z" * 40, 12345, [HEAD_SHA]]
        for bad in cases:
            body = {key: value for key, value in payload.items() if key != "head_sha"}
            if bad is not None:
                body["head_sha"] = bad
            with self.subTest(head_sha=bad):
                self.error("INVALID_INPUT", "submit_result", body)
        self.assertEqual("running", self.ok("get_ticket", self.scope())["state"])

    def test_submit_stores_the_head_sha_and_normalizes_case(self):
        """合法 sha 原样可读回；大写输入的存小写，比对才不用猜大小写。"""
        self.ticket()
        attempt = self.running()
        self.ok("submit_result", self.result_payload(attempt, head_sha=HEAD_SHA.upper()))
        ticket = self.ok("get_ticket", self.scope())
        self.assertEqual(HEAD_SHA, ticket["submissions"][0]["head_sha"])

    def test_submit_has_receipt_but_not_done(self):
        self.ticket()
        attempt = self.running()
        payload = self.result_payload(attempt)
        receipt = self.ok("submit_result", payload, "submission-1")
        self.assertEqual("awaiting_review", receipt["state"])
        self.assertEqual(receipt, self.ok("submit_result", payload, "submission-1"))
        ticket = self.ok("get_ticket", self.scope())
        self.assertEqual("awaiting_review", ticket["state"])
        self.assertEqual(receipt["submission_id"], ticket["submissions"][0]["id"])
        self.assertEqual(64, len(ticket["submissions"][0]["artifacts"][0]["sha256"]))
        self.error("INVALID_STATE", "submit_result", payload)

    def test_submit_wrong_session_project_or_attempt_rejected(self):
        self.ticket()
        attempt = self.running()
        payload = self.result_payload(attempt)
        for key, value, code in [("session_id", "other", "SESSION_MISMATCH"),
                                 ("project_id", "other", "PROJECT_MISMATCH"),
                                 ("attempt_id", "old", "STALE_ATTEMPT")]:
            self.error(code, "submit_result", {**payload, key: value})
        self.assertEqual("running", self.ok("get_ticket", self.scope())["state"])

    def test_submit_rejects_missing_or_external_files(self):
        self.ticket()
        attempt = self.running()
        payload = self.result_payload(attempt)
        (self.root / "outside.md").write_text("outside")
        for paths, code in [([], "INVALID_INPUT"), (["absent"], "FILE_NOT_FOUND"),
                            (["../outside.md"], "PATH_OUTSIDE_PROJECT"),
                            (["result.md", "./result.md"], "INVALID_INPUT")]:
            self.error(code, "submit_result", {**payload, "artifacts": paths})

    def test_changed_prd_blocks_submit_until_explicit_revision(self):
        self.ticket()
        attempt = self.running()
        payload = self.result_payload(attempt)
        (self.project / "PRD.md").write_text("需求第二版", encoding="utf-8")
        self.error("STALE_PRD", "submit_result", payload)
        # 要作废在飞的尝试、换一代次，就显式写 restart: true（PRD-013 R6）；
        # 不写它时 revise 只推判据版本，旧尝试照旧能交成果。
        revised = self.ok("revise_ticket", self.scope(expected_revision=1, reason="用户修改需求", prd_path="PRD.md",
                          criteria=[{"id": "C2", "text": "含 `python tools/run_tests.py` 且退出码 0"}],
                          restart=True))
        self.assertEqual(2, revised["revision"])
        self.error("STALE_ATTEMPT", "submit_result", payload)
        self.ok("claim_ticket", self.claim_payload(expected_revision=2))

    def test_recovery_invalidates_old_attempt_and_preserves_history(self):
        self.ticket()
        attempt = self.running()
        payload = self.result_payload(attempt)
        recovered = self.ok("recover_ticket", self.scope(expected_revision=1, attempt_id=attempt,
                                                     reason="已核对旧会话与实际产物"))
        self.assertEqual(2, recovered["revision"])
        self.assertEqual(attempt, recovered["superseded_attempt_id"])
        self.error("STALE_ATTEMPT", "submit_result", payload)
        self.error("STALE_REVISION", "claim_ticket", self.claim_payload(expected_revision=1))
        next_attempt = self.ok("claim_ticket", self.claim_payload(expected_revision=2))
        self.assertNotEqual(attempt, next_attempt["attempt_id"])
        self.assertEqual("superseded", self.ok("get_ticket", self.scope())["attempts"][0]["state"])

    def test_cancel_blocks_claim_and_recovery(self):
        self.ticket()
        self.ok("cancel_ticket", self.scope(expected_revision=1, reason="用户取消"))
        self.error("INVALID_STATE", "claim_ticket", self.claim_payload(expected_revision=2))
        self.error("INVALID_STATE", "recover_ticket", self.scope(expected_revision=2, reason="不能自动恢复",
                   attempt_id="missing"))
        self.error("INVALID_STATE", "revise_ticket", self.scope(expected_revision=2, reason="不能自动恢复",
                   prd_path="PRD.md", criteria=[{"id": "C1", "text": "含 `python -m task_center --help` 且退出码 0"}]))

    def test_recovery_requires_reason_and_expected_revision(self):
        self.ticket()
        attempt = self.running()
        self.error("INVALID_INPUT", "recover_ticket", self.scope(expected_revision=1, attempt_id=attempt, reason=""))
        self.error("STALE_REVISION", "recover_ticket", self.scope(expected_revision=2, attempt_id=attempt, reason="错误版本"))
        self.error("INVALID_INPUT", "recover_ticket", self.scope(expected_revision=1, reason="缺少尝试编号"))

    def submitted(self):
        self.ticket()
        attempt = self.running()
        submission = self.ok("submit_result", self.result_payload(attempt))
        return attempt, submission["submission_id"]

    def review_payload(self, attempt, submission, verdict="accept"):
        return self.scope(attempt_id=attempt, submission_id=submission, reviewer_session_id="reviewer-1",
                          verdict=verdict, checks=[{"id": "C1", "passed": verdict == "accept", "evidence": "已阅读清单并核对步骤"}],
                          reason="逐项检查后作出结论")

    def test_late_recovery_intent_cannot_supersede_new_attempt(self):
        attempt, sid = self.submitted()
        self.ok("review_result", self.review_payload(attempt, sid, "reject"))
        current = self.running("executor-2")
        self.error("STALE_ATTEMPT", "recover_ticket",
                   self.scope(expected_revision=1, attempt_id=attempt, reason="已核对 exec1 的副作用"))
        result = self.ok("get_ticket", self.scope())
        self.assertEqual("running", result["state"])
        self.assertEqual("running", [a for a in result["attempts"] if a["id"] == current][0]["state"])
        self.assertEqual(current, result["current_attempt"])

    def test_recovery_reports_discarded_submission(self):
        attempt, sid = self.submitted()
        receipt = self.ok("recover_ticket", self.scope(expected_revision=1, attempt_id=attempt,
                                                      reason="成果作废重做"))
        self.assertEqual(sid, receipt["discarded_submission_id"])
        self.assertEqual(attempt, receipt["superseded_attempt_id"])

    def test_revision_preserves_terminated_attempt_states(self):
        attempt, sid = self.submitted()
        self.ok("review_result", self.review_payload(attempt, sid))
        revised = self.ok("revise_ticket", self.scope(expected_revision=1, reason="补充验收项",
                          prd_path="PRD.md", criteria=[{"id": "C2", "text": "含 `python tools/run_tests.py` 且退出码 0"}], reopen=True))
        self.assertEqual("done", revised["previous_state"])
        result = self.ok("get_ticket", self.scope())
        self.assertEqual("ready", result["state"])
        self.assertEqual("accepted", result["attempts"][0]["state"])
        self.assertEqual("accept", result["reviews"][0]["verdict"])

    def test_rejected_attempt_state_survives_revision(self):
        attempt, sid = self.submitted()
        self.ok("review_result", self.review_payload(attempt, sid, "reject"))
        self.ok("revise_ticket", self.scope(expected_revision=1, reason="换写法",
                 prd_path="PRD.md", criteria=[{"id": "C2", "text": "含 `python tools/run_tests.py` 且退出码 0"}]))
        self.assertEqual("rejected", self.ok("get_ticket", self.scope())["attempts"][0]["state"])

    def test_revising_done_ticket_requires_explicit_reopen(self):
        attempt, sid = self.submitted()
        self.ok("review_result", self.review_payload(attempt, sid))
        payload = self.scope(expected_revision=1, reason="改错别字", prd_path="PRD.md",
                             criteria=[{"id": "C2", "text": "含 `python tools/run_tests.py` 且退出码 0"}])
        self.error("INVALID_STATE", "revise_ticket", payload)
        self.assertEqual("done", self.ok("get_ticket", self.scope())["state"])
        self.ok("revise_ticket", {**payload, "reopen": True})

    def test_revision_receipt_and_attempts_keep_prd_location(self):
        self.ticket()
        (self.project / "PRD-v2.md").write_text("需求第二版", encoding="utf-8")
        # 换需求文件再换一代人做：restart 显式要求作废，revision 才 +1（PRD-013 R6）。
        revised = self.ok("revise_ticket", self.scope(expected_revision=1, reason="换需求文件",
                          prd_path="PRD-v2.md", restart=True,
                          criteria=[{"id": "C2", "text": "含 `python tools/run_tests.py` 且退出码 0"}]))
        self.assertEqual("PRD-v2.md", revised["prd_path"])
        claim = self.ok("claim_ticket", self.claim_payload(expected_revision=2))
        attempt = [a for a in self.ok("get_ticket", self.scope())["attempts"] if a["id"] == claim["attempt_id"]][0]
        self.assertEqual("PRD-v2.md", attempt["prd_path"])
        self.assertEqual(revised["prd_sha256"], attempt["prd_sha256"])

    def test_deleted_artifact_reports_artifact_changed(self):
        attempt, sid = self.submitted()
        (self.project / "result.md").unlink()
        self.error("ARTIFACT_CHANGED", "review_result", self.review_payload(attempt, sid))

    def test_deeply_nested_payload_returns_structured_error(self):
        # Must pass field-name validation so packed() is actually reached (20000 levels).
        nested = []
        for _ in range(20000):
            nested = [nested]
        result = self.center.execute("create_ticket", {
            "project_id": "p1", "title": "深层嵌套",
            "deliverable": "一份清单",
            "prd_path": "PRD.md", "criteria": nested,
        }, "deep-1")
        self.assertFalse(result["ok"])
        self.assertEqual("INVALID_INPUT", result["error"]["code"])

    def test_init_reports_current_schema_version(self):
        fresh = self.root / "fresh.sqlite3"
        result = Center(fresh).execute("init", {})
        self.assertEqual(SCHEMA_VERSION, result["result"]["schema_version"])

    def test_current_shape_with_old_version_header_is_rejected(self):
        # v2 shape but user_version=1: only the version gate can reject this.
        conn = sqlite3.connect(self.db)
        before = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()
        destination = self.root / "v1-header-backup.sqlite3"
        cases = [("init", {}), ("list_tickets", {}),
                 ("get_ticket", {"ticket_id": "t1", "project_id": "p1"}),
                 ("backup", {"destination": str(destination)})]
        # Collect every entry point before asserting, so one failure cannot hide the next.
        results = {op: self.center.execute(op, payload, f"v1-header-{op}") for op, payload in cases}
        for operation, result in results.items():
            with self.subTest(operation=operation):
                self.assertFalse(result["ok"], result)
                self.assertEqual("SCHEMA_MISMATCH", result["error"]["code"], result)
                self.assertNotIn("schema_version", json.dumps(result))
        self.assertFalse(destination.exists())
        conn = sqlite3.connect(self.db)
        self.assertEqual(before, conn.execute("PRAGMA journal_mode").fetchone()[0])
        self.assertEqual(1, conn.execute("PRAGMA user_version").fetchone()[0])
        conn.close()

    def test_backup_never_claims_a_version_for_old_header(self):
        conn = sqlite3.connect(self.db)
        before_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()
        destination = self.root / "v1-header-backup.sqlite3"
        result = self.center.execute("backup", {"destination": str(destination)}, "backup-v1-header")
        self.assertFalse(result["ok"], result)
        self.assertEqual("SCHEMA_MISMATCH", result["error"]["code"], result)
        # A rejected backup must not declare any version, not even a downgraded one.
        self.assertNotIn("result", result)
        self.assertNotIn("schema_version", json.dumps(result))
        self.assertFalse(destination.exists())
        self.assertEqual([], [p.name for p in self.root.iterdir() if p.name.startswith(".task-backup-")])
        conn = sqlite3.connect(self.db)
        self.assertEqual(before_mode, conn.execute("PRAGMA journal_mode").fetchone()[0])
        self.assertEqual(1, conn.execute("PRAGMA user_version").fetchone()[0])
        conn.close()

    def test_old_schema_version_is_rejected_loudly(self):
        old = self.root / "old.sqlite3"
        conn = sqlite3.connect(old)
        for table in ("projects", "tickets", "attempts", "submissions", "reviews", "events", "requests"):
            conn.execute(f"CREATE TABLE {table}(id TEXT)")
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()
        result = Center(old).execute("list_tickets", {})
        self.assertEqual("SCHEMA_MISMATCH", result["error"]["code"])

    def test_backup_rejects_foreign_database_claiming_our_version(self):
        foreign = self.root / "foreign.sqlite3"
        conn = sqlite3.connect(foreign)
        conn.execute("CREATE TABLE unrelated(id INTEGER)")
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()
        destination = self.root / "foreign-backup.sqlite3"
        result = Center(foreign).execute("backup", {"destination": str(destination)}, "backup-foreign")
        self.assertEqual("SCHEMA_MISMATCH", result["error"]["code"])
        self.assertFalse(destination.exists())

    def test_independent_review_accepts_and_records_evidence(self):
        attempt, sid = self.submitted()
        payload = self.review_payload(attempt, sid)
        receipt = self.ok("review_result", payload, "review-once")
        self.assertEqual("done", receipt["state"])
        self.assertEqual(receipt, self.ok("review_result", payload, "review-once"))
        result = self.ok("get_ticket", self.scope())
        self.assertEqual(1, len(result["reviews"]))
        self.assertEqual("accept", result["reviews"][0]["verdict"])
        self.error("INVALID_STATE", "review_result", payload)

    def test_executor_cannot_review_own_work(self):
        attempt, sid = self.submitted()
        self.error("SELF_REVIEW", "review_result", {**self.review_payload(attempt, sid), "reviewer_session_id": "executor-1"})
        self.assertEqual("awaiting_review", self.ok("get_ticket", self.scope())["state"])

    def test_review_requires_complete_typed_evidence(self):
        attempt, sid = self.submitted()
        payload = self.review_payload(attempt, sid)
        for checks in [[], [{"id": "wrong", "passed": True, "evidence": "检查"}],
                       [{"id": "C1", "passed": 1, "evidence": "检查"}],
                       [{"id": "C1", "passed": True, "evidence": ""}],
                       [{"id": "C1", "passed": True, "evidence": "检查"}] * 2]:
            self.error("INVALID_INPUT", "review_result", {**payload, "checks": checks})
        self.error("INVALID_INPUT", "review_result", {**payload, "verdict": "accept", "checks": [{"id": "C1", "passed": False, "evidence": "未通过"}]})
        self.error("INVALID_INPUT", "review_result", {**payload, "verdict": "reject"})

    def test_artifact_or_prd_changes_block_acceptance(self):
        attempt, sid = self.submitted()
        payload = self.review_payload(attempt, sid)
        (self.project / "result.md").write_text("被改动的成果", encoding="utf-8")
        self.error("ARTIFACT_CHANGED", "review_result", payload)
        (self.project / "result.md").write_text("1. 检查步骤", encoding="utf-8")
        (self.project / "PRD.md").write_text("新需求", encoding="utf-8")
        self.error("STALE_PRD", "review_result", payload)

    def test_reject_rework_accept_keeps_both_attempts(self):
        attempt, sid = self.submitted()
        self.ok("review_result", self.review_payload(attempt, sid, "reject"))
        self.assertEqual("rework", self.ok("get_ticket", self.scope())["state"])
        new_attempt = self.running("executor-2")
        new_submission = self.ok("submit_result", self.result_payload(new_attempt, "executor-2"))["submission_id"]
        self.error("STALE_ATTEMPT", "review_result", self.review_payload(attempt, sid))
        self.error("SUBMISSION_MISMATCH", "review_result", self.review_payload(new_attempt, sid))
        self.error("SELF_REVIEW", "review_result", {**self.review_payload(new_attempt, new_submission), "reviewer_session_id": "executor-1"})
        self.ok("review_result", self.review_payload(new_attempt, new_submission))
        result = self.ok("get_ticket", self.scope())
        self.assertEqual("done", result["state"])
        self.assertEqual(2, len(result["attempts"]))
        self.assertEqual(2, len(result["reviews"]))

    def test_cancelled_submission_cannot_be_accepted(self):
        attempt, sid = self.submitted()
        self.ok("cancel_ticket", self.scope(expected_revision=1, reason="需求取消"))
        self.error("STALE_ATTEMPT", "review_result", self.review_payload(attempt, sid))

    def test_consistent_backup_is_queryable_and_never_overwrites(self):
        self.ticket()
        backup = self.root / "backup.sqlite3"
        self.ok("backup", {"destination": str(backup)})
        result = Center(backup).execute("get_ticket", {"project_id": "p1", "ticket_id": self.tid("t1")})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.tid("t1"), result["result"]["id"])
        self.error("BACKUP_EXISTS", "backup", {"destination": str(backup)})
        self.error("BACKUP_EXISTS", "backup", {"destination": str(self.db)})

    def test_file_limit_is_enforced_before_hashing(self):
        self.ticket()
        attempt = self.running()
        payload = self.result_payload(attempt)
        with (self.project / "large.bin").open("wb") as out:
            out.truncate(32 * 1024 * 1024 + 1)
        self.error("FILE_TOO_LARGE", "submit_result", {**payload, "artifacts": ["large.bin"]})

    def test_foreign_database_init_is_rejected_without_modification(self):
        foreign = self.root / "foreign.sqlite3"
        conn = sqlite3.connect(foreign)
        conn.execute("CREATE TABLE unrelated(value TEXT)")
        conn.commit()
        before = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        result = Center(foreign).execute("init", {})
        self.assertEqual("SCHEMA_MISMATCH", result["error"]["code"])
        conn = sqlite3.connect(foreign)
        after = conn.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertEqual(before, after)
        self.assertEqual({"unrelated"}, tables)

    def test_fake_version_header_is_rejected(self):
        foreign = self.root / "fake.sqlite3"
        conn = sqlite3.connect(foreign)
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()
        result = Center(foreign).execute("init", {})
        self.assertEqual("SCHEMA_MISMATCH", result["error"]["code"])
        conn = sqlite3.connect(foreign)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertEqual(set(), tables)

    def test_directory_symlink_escape_is_rejected(self):
        outside = self.root / "outside-dir"
        outside.mkdir()
        (outside / "PRD.md").write_text("外部需求", encoding="utf-8")
        link = self.project / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are unavailable on this system")
        self.error("PATH_OUTSIDE_PROJECT", "create_ticket", {
            "project_id": "p1", "title": "任务",
            "deliverable": "一份清单",
            "prd_path": "linked/PRD.md", "criteria": [{"id": "C1", "text": "含 `python -m task_center --help` 且退出码 0"}],
        })

    def test_request_id_cannot_be_reused_across_operations(self):
        self.ticket()
        self.ok("register_project", {"project_id": "p9", "name": "九号", "root": str(self.root)}, "shared-id")
        self.error("REQUEST_CONFLICT", "create_ticket", {
            "project_id": "p9", "title": "跨操作复用",
            "deliverable": "一份清单",
            "prd_path": "center.sqlite3.md", "criteria": [{"id": "C1", "text": "含 `python -m task_center --help` 且退出码 0"}],
        }, "shared-id")

    def test_non_utf8_text_names_the_encoding_problem(self):
        self.ticket()
        attempt = self.running()
        payload = self.result_payload(attempt)
        result = self.call("submit_result", {**payload, "summary": "缺少\udc80回滚步骤"})
        self.assertFalse(result["ok"], result)
        self.assertEqual("INVALID_INPUT", result["error"]["code"])
        self.assertIn("UTF-8", result["error"]["message"])
        self.assertEqual("running", self.ok("get_ticket", self.scope())["state"])

    def test_absolute_artifact_reference_rejected(self):
        self.ticket()
        attempt = self.running()
        payload = self.result_payload(attempt)
        self.error("PATH_OUTSIDE_PROJECT", "submit_result", {**payload, "artifacts": [str(self.project / "result.md")]})


if __name__ == "__main__":
    unittest.main()
