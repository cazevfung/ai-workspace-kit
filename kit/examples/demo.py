"""Temporary-data demonstration, not a real Cindy session integration test."""
import json
import sys
import tempfile
from pathlib import Path

from task_center import Center


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="cindy-task-demo-") as temp:
        root = Path(temp)
        project = root / "试点项目"
        project.mkdir()
        (project / "PRD.md").write_text("成果必须包含两个检查步骤。", encoding="utf-8")
        (project / "result.md").write_text("1. 检查输入", encoding="utf-8")
        center = Center(root / "demo.sqlite3")
        seq = 0

        def call(op, **payload):
            nonlocal seq
            seq += 1
            result = center.execute(op, payload, f"demo-{seq}")
            if not result["ok"]:
                raise RuntimeError(result)
            print(json.dumps({"operation": op, "result": result["result"]}, ensure_ascii=False))
            return result["result"]

        call("init")
        call("register_project", project_id="demo", name="模拟试点项目", root=str(project))
        scope = {"project_id": "demo", "ticket_id": "DEMO-1"}
        call("create_ticket", **scope, title="制作两项检查清单", prd_path="PRD.md",
             deliverable="一份含两个可勾选步骤的检查清单",
             criteria=[{"id": "C1", "text": "至少包含两个检查步骤"}])
        for cycle in (1, 2):
            aid = call("claim_ticket", **scope, expected_revision=1,
                       coordinator_session_id=f"SIMULATED-coordinator-{cycle}")["attempt_id"]
            session = f"SIMULATED-executor-{cycle}"
            call("bind_session", **scope, attempt_id=aid, session_id=session, project_root=str(project))
            if cycle == 2:
                (project / "result.md").write_text("1. 检查输入\n2. 检查输出", encoding="utf-8")
            sid = call("submit_result", **scope, attempt_id=aid, session_id=session, summary="模拟执行提交",
                       artifacts=["result.md"], evidence=["演示数据，不代表真实会话执行"], risks=[],
                       head_sha="0000000000000000000000000000000000000000")["submission_id"]
            passed = len((project / "result.md").read_text(encoding="utf-8").splitlines()) >= 2
            call("review_result", **scope, attempt_id=aid, submission_id=sid,
                 reviewer_session_id="SIMULATED-reviewer", verdict="accept" if passed else "reject",
                 checks=[{"id": "C1", "passed": passed, "evidence": "演示程序实际读取文件并检查行数"}],
                 reason="满足两项要求" if passed else "只有一项，需要返工")
        final = call("get_ticket", **scope)
        assert final["state"] == "done" and len(final["reviews"]) == 2
        print("演示结束：一次拒收、一次返工、一次通过；全部数据位于临时目录。")


if __name__ == "__main__":
    main()
