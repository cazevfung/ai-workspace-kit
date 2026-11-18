"""AC09 演练：两个项目、两条会话、真实重叠，打印时间线。

    python examples/dual_project_drill.py

只读一个临时库，跑完自动删除；口径与 tests/test_concurrency.py 一致。
"""
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from task_center import Center


def run(center, operation, payload):
    result = center.execute(operation, payload, request_id=f"{operation}-{abs(hash(str(payload)))}")
    assert result["ok"], result
    return result["result"]


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        projects = {"alpha": root / "项目甲", "beta": root / "项目乙"}
        for path in projects.values():
            path.mkdir()
            (path / "PRD.md").write_text("需求第一版", encoding="utf-8")
        db = root / "center.sqlite3"
        center = Center(db)
        run(center, "init", {})
        for pid, path in projects.items():
            run(center, "register_project", {"project_id": pid, "name": pid, "root": str(path)})
            run(center, "create_ticket", {"ticket_id": f"{pid}-t1", "project_id": pid, "title": "整理检查清单",
                                          "deliverable": "一份逐项可勾选的检查清单", "prd_path": "PRD.md",
                                          "criteria": [{"id": "C1", "text": "包含明确的检查步骤"}]})
        barrier = threading.Barrier(2)

        def work(pid):
            session = f"exec-{pid}"
            claim = run(center, "claim_ticket", {"project_id": pid, "ticket_id": f"{pid}-t1",
                                                "expected_revision": 1,
                                                "coordinator_session_id": f"coord-{pid}"})
            run(center, "bind_session", {"project_id": pid, "ticket_id": f"{pid}-t1",
                                         "attempt_id": claim["attempt_id"], "session_id": session,
                                         "project_root": str(projects[pid])})
            barrier.wait()
            (projects[pid] / "result.md").write_text(f"{pid} 的清单", encoding="utf-8")
            run(center, "submit_result", {"project_id": pid, "ticket_id": f"{pid}-t1",
                                          "attempt_id": claim["attempt_id"], "session_id": session,
                                          "artifacts": ["result.md"], "summary": f"{pid} 的清单",
                                          "evidence": ["按 PRD 逐条核对"], "risks": []})

        threads = [threading.Thread(target=work, args=(pid,)) for pid in projects]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        windows = {}
        for pid in projects:
            events = run(center, "events", {"project_id": pid, "ticket_id": f"{pid}-t1"})["items"]
            stamps = {item["operation"]: datetime.fromisoformat(item["created_at"]) for item in events}
            windows[pid] = (stamps["bind_session"], stamps["submit_result"])
        for pid, (start, end) in windows.items():
            print(f"{pid}: running {start.isoformat()} → submitted {end.isoformat()}")
        start = max(item[0] for item in windows.values())
        end = min(item[1] for item in windows.values())
        print(f"重叠区间: {start.isoformat()} → {end.isoformat()} ({(end - start).total_seconds()} s)")


if __name__ == "__main__":
    main()
