"""Local trusted-user task commands. No host identity or session dispatch is implied."""
import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from .artifacts import fingerprint, project_root
from .db import Problem, backup_database, connection, initialize, migrate_database, now, packed, require

FIELDS = {
    "init": set(),
    "migrate": set(),
    "backup": {"destination"},
    "review_result": {"project_id", "ticket_id", "attempt_id", "submission_id", "reviewer_session_id", "verdict", "checks", "reason"},
    "register_project": {"project_id", "name", "root"},
    "unregister_project": {"project_id"},
    "list_projects": set(),
    "create_ticket": {"project_id", "ticket_id", "title", "deliverable", "prd_path", "criteria", "root"},
    "get_ticket": {"project_id", "ticket_id"},
    "list_tickets": {"project_id", "limit", "offset"},
    "events": {"project_id", "ticket_id", "limit", "offset"},
    "claim_ticket": {"project_id", "ticket_id", "expected_revision", "coordinator_session_id",
                     "coordinator_workdir", "allow_parallel"},
    "bind_session": {"project_id", "ticket_id", "attempt_id", "session_id", "project_root"},
    "submit_result": {"project_id", "ticket_id", "attempt_id", "session_id", "summary", "artifacts", "evidence", "risks",
                      "head_sha"},
    "recover_ticket": {"project_id", "ticket_id", "attempt_id", "expected_revision", "reason"},
    "revise_ticket": {"project_id", "ticket_id", "expected_revision", "reason", "prd_path", "criteria", "deliverable", "reopen"},
    "cancel_ticket": {"project_id", "ticket_id", "expected_revision", "reason"},
    "merge_ticket": {"project_id", "ticket_id", "commit", "target_branch", "tests", "reviewed_sha"},
    "list_unmerged": {"project_id"},
    "ask_ticket": {"project_id", "ticket_id", "attempt_id", "session_id", "question", "options"},
    "answer_ticket": {"project_id", "ticket_id", "question_id", "answer", "answered_by",
                      "answered_by_session_id", "prd_sha256"},
    "record_notice": {"project_id", "ticket_id", "attempt_id", "session_id", "origin_session_id",
                      "status", "detail", "target_session_id", "wake_kind"},
}
READS = {"get_ticket", "list_tickets", "list_projects", "list_unmerged", "events"}

# 回信失败不改工单状态，也不自动重派：它在回执上带这一个标记，等人来看（PRD-009 R5 / AC07）。
NOTIFY_FAILED = "NOTIFY_FAILED"
# 宿主把「发给某个会话」当成新建任务时用的标记：那次回执没有到达任何人，命令要非零退出。
# 只报错不停会话：自动停掉误建的新会话要调宿主能力，超出本单范围。
NOTIFY_CREATED_TASK = "NOTIFY_CREATED_TASK"

# 同项目一次只跑一张单：这四种状态都算「在途」，其中 awaiting_review 也拦，
# 意味着「验收完、合并前不能开下一张」（AW-061）。done/cancelled 与还没被领的
# ready/rework 都不算在途——rework 是可领取状态，它自己复工时同样要排队。
ACTIVE_TICKET_STATES = ("dispatching", "running", "awaiting_review", "blocked")


def text(data, key, max_length=4000):
    value = data.get(key)
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= max_length,
            "INVALID_INPUT", f"{key} must be a nonempty string of at most {max_length} characters")
    return value


def wake_kind(data, max_length=40):
    """`wake_kind` 是宿主给的，不是本工具的白名单。

    宿主实际会回 `already-active` 这类枚举外的值（AW-060 验收回执里出现过）。
    只认三个值就等于逼调用方挑一个近似的填，库里留下的是推定而不是事实。
    所以这里只校验形状：非空、不含空白或换行、不超过 40 字；取值原样入库。
    """
    value = data.get("wake_kind")
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= max_length,
            "INVALID_INPUT", f"wake_kind must be a nonempty string of at most {max_length} characters")
    require(not any(character.isspace() for character in value), "INVALID_INPUT",
            "wake_kind must be one token without internal whitespace or newlines")
    return value


def integer(data, key, default=None, minimum=0, maximum=100):
    value = data.get(key, default)
    require(type(value) is int and minimum <= value <= maximum,
            "INVALID_INPUT", f"{key} must be an integer from {minimum} to {maximum}")
    return value


def criteria(data):
    value = data.get("criteria")
    require(isinstance(value, list) and 1 <= len(value) <= 100, "INVALID_INPUT", "Provide 1 to 100 criteria")
    ids = set()
    for item in value:
        require(isinstance(item, dict) and set(item) == {"id", "text"}, "INVALID_INPUT", "Criterion requires id and text")
        cid = text(item, "id", 100)
        text(item, "text")
        require(cid not in ids, "INVALID_INPUT", "Criterion ids must be unique")
        ids.add(cid)
    return value


def string_list(data, key, minimum=0, maximum=100):
    value = data.get(key)
    require(isinstance(value, list) and minimum <= len(value) <= maximum,
            "INVALID_INPUT", f"{key} must contain {minimum} to {maximum} strings")
    for item in value:
        text({key: item}, key)
    return value


def options(data):
    # 选项不是答案的枚举约束：`answer` 是自由文本，可以选 A，也可以写「都不选，改成 C」。
    value = data.get("options", [])
    require(isinstance(value, list) and 0 <= len(value) <= 6, "INVALID_INPUT", "options must contain 0 to 6 entries")
    ids = set()
    for item in value:
        require(isinstance(item, dict) and set(item) == {"id", "text"}, "INVALID_INPUT", "Option requires id and text")
        oid = text(item, "id", 20)
        text(item, "text", 200)
        require(oid not in ids, "INVALID_INPUT", "Option ids must be unique")
        ids.add(oid)
    return value


def digest_text(data, key):
    # 需求摘要的格式先校一次：拿到长度不对的值去比对，会报成「需求变了」而不是「字段写错了」。
    value = text(data, key, 64)
    require(len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value),
            "INVALID_INPUT", f"{key} must be 64 hexadecimal characters")
    return value


# 老提交（schema 8 以前写下、没有 head_sha 那一列）合并时唯一能填的值。
# 它不是白名单里的「另一种 sha」，而是「这句话我没法核对」的如实标注。
UNVERIFIED = "unverified"


def sha_text(data, key, max_length=40):
    """提交 sha：40 位十六进制，大小写都收，存小写。

    只校形状。sha 本身对不对这里验不了（本工具不跑 git），也不该猜：验收会话拿它去
    `git rev-parse HEAD` 核，`tools/auto_merge.py` 拿它去比分支 tip。
    """
    value = text(data, key, max_length)
    require(len(value) == 40 and all(c in "0123456789abcdefABCDEF" for c in value),
            "INVALID_INPUT", f"{key} must be 40 hexadecimal characters")
    return value.lower()


def latest_head_sha(conn, ticket_id):
    """这张单最新一次提交记下的 head sha；没有提交或那行是旧数据时返回 None。

    取「最新」而不是「和这次尝试对得上的那条」：验收通过之后又提交一次时，被验收的是
    当时那条，而合并时要比的就是它——把较旧的一条拿来比，恰好会把那次多出来的提交放过。
    """
    row = conn.execute(
        "SELECT s.head_sha FROM submissions s JOIN attempts a ON a.id=s.attempt_id"
        " WHERE a.ticket_id=? ORDER BY s.created_at DESC, s.rowid DESC LIMIT 1",
        (ticket_id,)).fetchone()
    return None if row is None else row["head_sha"]


def ticket_dict(row):
    value = dict(row)
    value["criteria"] = json.loads(value["criteria"])
    return value


class Center:
    def __init__(self, db_path):
        self.path = Path(db_path).absolute()

    def execute(self, operation, payload=None, request_id=None):
        try:
            require(isinstance(operation, str) and operation in FIELDS, "INVALID_INPUT", "Unknown operation")
            payload = {} if payload is None else payload
            require(isinstance(payload, dict), "INVALID_INPUT", "Payload must be an object")
            require(not (set(payload) - FIELDS[operation]), "INVALID_INPUT", "Unknown payload fields")
            if operation == "init":
                return {"ok": True, "result": initialize(self.path)}
            if operation == "migrate":
                return {"ok": True, "result": migrate_database(self.path)}
            if operation == "backup":
                return {"ok": True, "result": backup_database(self.path, text(payload, "destination"))}
            write = operation not in READS
            if write:
                text({"request_id": request_id}, "request_id", 200)
            digest = hashlib.sha256(packed({"operation": operation, "payload": payload}).encode("utf-8")).hexdigest()
            with connection(self.path, write=write) as conn:
                if write:
                    saved = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
                    if saved:
                        require(saved["fingerprint"] == digest, "REQUEST_CONFLICT", "Request id was already used for different parameters")
                        return json.loads(saved["response"])
                result = getattr(self, "_" + operation)(conn, payload)
                # 已落库但仍算失败的调用（`wake_kind=created` 要记一条 failed 再非零退出）：
                # 行必须留下来，命令必须失败，所以在同一张事务里先把最终回执算完再提交，
                # 并把这份回执原样存进 requests——用同一个 request_id 重试会拿到同一份失败回执。
                rejected = result.pop("_reject", None) if isinstance(result, dict) else None
                response = {"ok": True, "result": result}
                if rejected is not None:
                    response = {"ok": False, "error": rejected, "result": result}
                if write:
                    conn.execute("INSERT INTO requests VALUES (?,?,?)", (request_id, digest, packed(response)))
                    conn.execute("INSERT INTO events(ticket_id,operation,detail,created_at) VALUES (?,?,?,?)",
                                 (payload.get("ticket_id"), operation, packed({"request_id": request_id, "result": result}), now()))
                return response
        except Problem as exc:
            return {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        except sqlite3.IntegrityError:
            return {"ok": False, "error": {"code": "CONFLICT", "message": "Conflicting project, ticket, session or result"}}
        except sqlite3.Error as exc:
            return {"ok": False, "error": {"code": "STORAGE_ERROR", "message": str(exc)}}
        except UnicodeEncodeError:
            return {"ok": False, "error": {"code": "INVALID_INPUT",
                                           "message": "Text cannot be encoded as UTF-8; write the JSON input as UTF-8 (ASCII-escaped JSON is safest)"}}
        except (ValueError, TypeError, OSError, RecursionError):
            return {"ok": False, "error": {"code": "INVALID_INPUT", "message": "Invalid input or inaccessible local path"}}

    def _project(self, conn, pid):
        row = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        require(row is not None, "NOT_FOUND", "Project does not exist")
        return row

    def _ticket(self, conn, data):
        row = conn.execute("SELECT * FROM tickets WHERE id=?", (text(data, "ticket_id", 200),)).fetchone()
        require(row is not None, "NOT_FOUND", "Ticket does not exist")
        require(row["project_id"] == text(data, "project_id", 200), "PROJECT_MISMATCH", "Ticket belongs to a different project")
        return row

    def _register_project(self, conn, data):
        pid, name = text(data, "project_id", 200), text(data, "name", 200)
        root = project_root(data.get("root"))
        # root 是身份、project_id 是标签：同一路径重复登记复用原记录，不改已有标签。
        # 这里不报 CONFLICT，也不按新标签改名——标签给错了由 unregister_project 修正，
        # 不是把同一个目录记成两行。但一个标签不能同时指向两个目录（C4），所以
        # 传进来的 project_id 已经落在另一个目录上时仍然是 CONFLICT，且不动任一行。
        existing = conn.execute("SELECT * FROM projects WHERE root=?", (root,)).fetchone()
        taken = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if existing is not None and taken is not None and taken["root"] != root:
            raise Problem("CONFLICT", f"project_id {pid} is already registered at {taken['root']} and {root} "
                                      f"is registered as {existing['id']}; neither record is changed")
        if existing is not None:
            return {"project_id": existing["id"], "name": existing["name"], "root": existing["root"],
                    "reused": True}
        if taken is not None:
            raise Problem("CONFLICT", f"project_id {pid} is already registered at {taken['root']}; "
                                      "root is identity, so one id cannot point at a second directory")
        conn.execute("INSERT INTO projects VALUES (?,?,?)", (pid, name, root))
        return {"project_id": pid, "name": name, "root": root, "reused": False}

    def _list_projects(self, conn, data):
        rows = conn.execute("SELECT id, name, root FROM projects ORDER BY id")
        return {"items": [{"project_id": row["id"], "name": row["name"], "root": row["root"]} for row in rows]}

    def _unregister_project(self, conn, data):
        # 全局化之后每个会话都能登记项目，误登记必须能撤回；但有工单时不能撤，
        # 否则工单会指向一个不存在的项目，历史就断了。
        project = self._project(conn, text(data, "project_id", 200))
        tickets = [row["id"] for row in conn.execute(
            "SELECT id FROM tickets WHERE project_id=? ORDER BY created_at,id", (project["id"],))]
        require(not tickets, "PROJECT_HAS_TICKETS",
                "Project still owns tickets (" + ", ".join(tickets) + "); cancel or move them first")
        conn.execute("DELETE FROM projects WHERE id=?", (project["id"],))
        return {"project_id": project["id"], "name": project["name"], "root": project["root"], "unregistered": True}

    def _ticket_root(self, conn, pid, raw_root):
        """开单时的项目归属：列表即范围、登记只是记账。

        已登记的项目照常用；未登记但给了 root 就顺手记一行再开单，名字取目录名；
        已登记又给了不一致的 root 报 ROOT_MISMATCH 且不动原记录——root 是身份，不是可以随手改的字段。
        """
        row = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if row is not None:
            if raw_root is None:
                return row["root"]
            root = project_root(raw_root)
            require(root == row["root"],
                    "ROOT_MISMATCH",
                    f"Project {pid} is registered at {row['root']}; the given root differs and the record is left unchanged")
            return row["root"]
        require(raw_root is not None,
                "NOT_FOUND",
                f"Project {pid} does not exist; pass root to register it while creating the ticket")
        root = project_root(raw_root)
        owner = conn.execute("SELECT * FROM projects WHERE root=?", (root,)).fetchone()
        if owner is not None:
            raise Problem("ROOT_MISMATCH",
                          f"Root {root} is already registered as project {owner['id']}; "
                          "root is identity and project_id is a label, so use that project_id or unregister it first")
        name = Path(root).name or pid
        conn.execute("INSERT INTO projects VALUES (?,?,?)", (pid, name, root))
        return root

    def _create_ticket(self, conn, data):
        tid, pid = text(data, "ticket_id", 200), text(data, "project_id", 200)
        root = self._ticket_root(conn, pid, data.get("root"))
        title, checks = text(data, "title", 500), criteria(data)
        # 交付物主题不是可选备注：只约束验收条件、不约束成果主题时，成果会合规地跑偏。
        deliverable = text(data, "deliverable", 500)
        prd = fingerprint(root, text(data, "prd_path"))
        conn.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,1,'ready',NULL,?)",
                     (tid, pid, title, deliverable, prd["path"], prd["sha256"], packed(checks), now()))
        return ticket_dict(self._ticket(conn, data))

    def _get_ticket(self, conn, data):
        result = ticket_dict(self._ticket(conn, data))
        result["attempts"] = [dict(row) for row in conn.execute("SELECT * FROM attempts WHERE ticket_id=? ORDER BY rowid", (result["id"],))]
        submissions = conn.execute("SELECT s.* FROM submissions s JOIN attempts a ON a.id=s.attempt_id WHERE a.ticket_id=? ORDER BY s.rowid", (result["id"],))
        result["submissions"] = [dict(row) for row in submissions]
        for item in result["submissions"]:
            for key in ("artifacts", "evidence", "risks"):
                item[key] = json.loads(item[key])
        reviews = conn.execute("SELECT r.* FROM reviews r JOIN submissions s ON s.id=r.submission_id JOIN attempts a ON a.id=s.attempt_id WHERE a.ticket_id=? ORDER BY r.rowid", (result["id"],))
        result["reviews"] = [dict(row) for row in reviews]
        for item in result["reviews"]:
            item["checks"] = json.loads(item["checks"])
        result["merges"] = [dict(row) for row in conn.execute(
            "SELECT * FROM merges WHERE ticket_id=? ORDER BY rowid", (result["id"],))]
        # 问答随尝试挂在工单下面：验收会话靠它判断「成果是不是按最终结论做的」（PRD-005 3）。
        result["questions"] = [dict(row) for row in conn.execute(
            "SELECT * FROM questions WHERE ticket_id=? ORDER BY rowid", (result["id"],))]
        for item in result["questions"]:
            item["options"] = json.loads(item["options"])
        result["answers"] = [dict(row) for row in conn.execute(
            "SELECT a.* FROM answers a JOIN questions q ON q.id=a.question_id WHERE q.ticket_id=? ORDER BY a.rowid",
            (result["id"],))]
        # 回信记录随工单挂上来：发了没、失败原因、发给谁，验收会话读 get_ticket 就能判闭环（PRD-009 AC06）。
        result["notices"] = [dict(row) for row in conn.execute(
            "SELECT * FROM notices WHERE ticket_id=? ORDER BY rowid", (result["id"],))]
        return result

    def _list_tickets(self, conn, data):
        limit, offset = integer(data, "limit", 50, 1), integer(data, "offset", 0, 0, 1000000)
        pid = data.get("project_id")
        if pid is not None:
            text(data, "project_id", 200)
        rows = conn.execute("SELECT * FROM tickets WHERE (? IS NULL OR project_id=?) ORDER BY created_at,id LIMIT ? OFFSET ?",
                            (pid, pid, limit, offset))
        return {"items": [ticket_dict(row) for row in rows], "limit": limit, "offset": offset}

    def _unchanged_prd(self, conn, ticket):
        project = self._project(conn, ticket["project_id"])
        prd = fingerprint(project["root"], ticket["prd_path"])
        require(prd["sha256"] == ticket["prd_sha256"], "STALE_PRD", "PRD changed; explicitly revise the ticket before proceeding")
        return project

    def _expected_revision(self, ticket, data):
        revision = integer(data, "expected_revision", minimum=1, maximum=1000000000)
        require(ticket["revision"] == revision, "STALE_REVISION", "Ticket revision changed; read its current state")

    def _active(self, conn, data, check_session=False):
        ticket = self._ticket(conn, data)
        aid = text(data, "attempt_id", 200)
        require(ticket["current_attempt"] == aid, "STALE_ATTEMPT", "This is not the current execution attempt")
        attempt = conn.execute("SELECT * FROM attempts WHERE id=?", (aid,)).fetchone()
        require(attempt is not None and attempt["revision"] == ticket["revision"], "STALE_ATTEMPT", "Attempt no longer matches the ticket revision")
        if check_session:
            require(attempt["session_id"] == text(data, "session_id", 500), "SESSION_MISMATCH", "Result does not match the bound session")
        return ticket, attempt

    def _busy_tickets(self, conn, ticket):
        """同项目里别的在途工单：一次只跑一张，除非调用方显式说要并行。"""
        return conn.execute(
            "SELECT id, state FROM tickets WHERE project_id=? AND id<>? AND state IN (?,?,?,?) "
            "ORDER BY created_at,id",
            (ticket["project_id"], ticket["id"], *ACTIVE_TICKET_STATES)).fetchall()

    def _claim_ticket(self, conn, data):
        ticket = self._ticket(conn, data)
        self._expected_revision(ticket, data)
        require(ticket["state"] in ("ready", "rework"), "INVALID_STATE", "Ticket is not ready to be claimed")
        # 串行是默认值，不是礼貌建议：同项目还有在途工单就拒绝，并点名是哪一张、停在哪一步。
        # 越过要显式传 allow_parallel=true，越过时留一条事件，事后看得出这是故意的。
        allow_parallel = data.get("allow_parallel", False)
        require(type(allow_parallel) is bool, "INVALID_INPUT", "allow_parallel must be a boolean")
        busy = self._busy_tickets(conn, ticket)
        if busy and not allow_parallel:
            listed = ", ".join(f"{row['id']} ({row['state']})" for row in busy)
            raise Problem("DISPATCH_BUSY",
                          f"Project {ticket['project_id']} already has a ticket in flight: {listed}; "
                          "finish it (merge or cancel) before claiming another, or pass allow_parallel=true")
        parallel_with = [{"ticket_id": row["id"], "state": row["state"]} for row in busy]
        self._unchanged_prd(conn, ticket)
        # 派发者与执行者必须是两个会话：claim 时写下谁派的，bind 时拒绝同一个会话。
        coordinator = text(data, "coordinator_session_id", 500)
        # 协调者自己的工作目录是可选的自报值：不传就空，不阻止派发（PRD-009 R5）。
        workdir = text(data, "coordinator_workdir", 1000) if "coordinator_workdir" in data else None
        aid = str(uuid4())
        conn.execute("INSERT INTO attempts(id,ticket_id,revision,prd_path,prd_sha256,state,session_id,"
                     "coordinator_session_id,coordinator_workdir,created_at) VALUES (?,?,?,?,?,'dispatching',NULL,?,?,?)",
                     (aid, ticket["id"], ticket["revision"], ticket["prd_path"], ticket["prd_sha256"],
                      coordinator, workdir, now()))
        conn.execute("UPDATE tickets SET state='dispatching',current_attempt=? WHERE id=?", (aid, ticket["id"]))
        if parallel_with:
            conn.execute("INSERT INTO events(ticket_id,operation,detail,created_at) VALUES (?,?,?,?)",
                         (ticket["id"], "claim_ticket",
                          packed({"allow_parallel_override": True, "parallel_with": parallel_with}), now()))
        return {"ticket_id": ticket["id"], "project_id": ticket["project_id"], "attempt_id": aid,
                "revision": ticket["revision"], "state": "dispatching",
                "coordinator_session_id": coordinator, "coordinator_workdir": workdir,
                "allow_parallel": allow_parallel, "parallel_with": parallel_with}

    def _bind_session(self, conn, data):
        ticket, attempt = self._active(conn, data)
        require(ticket["state"] == "dispatching", "INVALID_STATE", "Attempt is not awaiting a session binding")
        # 协调会话不能执行自己派发的工单；拒绝必须发生在任何写入之前，工单停在 dispatching。
        session = text(data, "session_id", 500)
        require(session != attempt["coordinator_session_id"], "SELF_DISPATCH",
                "The coordinator that claimed this ticket cannot execute it; open a separate session")
        project = self._unchanged_prd(conn, ticket)
        require(project_root(data.get("project_root")) == project["root"], "PROJECT_MISMATCH", "Session project directory does not match the registered project")
        conn.execute("UPDATE attempts SET session_id=?,state='running' WHERE id=?", (session, attempt["id"]))
        conn.execute("UPDATE tickets SET state='running' WHERE id=?", (ticket["id"],))
        return {"attempt_id": attempt["id"], "session_id": session,
                "coordinator_session_id": attempt["coordinator_session_id"],
                "state": "running", "host_identity_verified": False}

    def _submit_result(self, conn, data):
        ticket, attempt = self._active(conn, data, check_session=True)
        require(ticket["state"] == "running", "INVALID_STATE", "Ticket is not accepting execution results")
        project = self._unchanged_prd(conn, ticket)
        paths = string_list(data, "artifacts", minimum=1, maximum=20)
        artifacts = [fingerprint(project["root"], path) for path in paths]
        require(len({str(Path(a["path"])).casefold() for a in artifacts}) == len(artifacts),
                "INVALID_INPUT", "Duplicate artifact paths")
        summary = text(data, "summary")
        evidence, risks = string_list(data, "evidence", minimum=1), string_list(data, "risks")
        # 被验收的那棵树要能事后核对：记下提交时的 head sha，合并时拿它比分支 tip。
        head_sha = sha_text(data, "head_sha")
        sid = str(uuid4())
        conn.execute("INSERT INTO submissions(id,attempt_id,summary,artifacts,evidence,risks,created_at,head_sha)"
                     " VALUES (?,?,?,?,?,?,?,?)",
                     (sid, attempt["id"], summary, packed(artifacts), packed(evidence), packed(risks), now(), head_sha))
        conn.execute("UPDATE attempts SET state='submitted' WHERE id=?", (attempt["id"],))
        conn.execute("UPDATE tickets SET state='awaiting_review' WHERE id=?", (ticket["id"],))
        return {"submission_id": sid, "attempt_id": attempt["id"], "state": "awaiting_review",
                "artifacts": artifacts, "head_sha": head_sha}

    def _ask_ticket(self, conn, data):
        # 提问也要走 _active：它同时挡住旧尝试（STALE_ATTEMPT）与换了标识的会话（SESSION_MISMATCH）。
        ticket, attempt = self._active(conn, data, check_session=True)
        require(ticket["state"] == "running", "INVALID_STATE",
                "Only a running ticket can be asked a question; a blocked one is already waiting for its answer")
        # ALREADY_ASKED 是兜底：正常情况下「工单 blocked」与「有 open 问题」总是成对出现，
        # 所以问答的常规路径先撞上面的状态检查；这条与 one_open_question 索引一起堵住不一致行。
        require(conn.execute("SELECT 1 FROM questions WHERE attempt_id=? AND state='open'",
                             (attempt["id"],)).fetchone() is None,
                "ALREADY_ASKED", "This attempt already has an open question; wait for its answer before asking another")
        question, choices, asked_at = text(data, "question", 2000), options(data), now()
        qid = "q-" + str(uuid4())
        conn.execute("INSERT INTO questions VALUES (?,?,?,?,?,'open',?)",
                     (qid, ticket["id"], attempt["id"], question, packed(choices), asked_at))
        # 两处都写，权威在工单级（PRD-005 4.4）；revision 不变：等一句话不是新代次。
        conn.execute("UPDATE attempts SET state='blocked' WHERE id=?", (attempt["id"],))
        conn.execute("UPDATE tickets SET state='blocked' WHERE id=?", (ticket["id"],))
        return {"question_id": qid, "ticket_id": ticket["id"], "attempt_id": attempt["id"],
                "state": "blocked", "attempt_state": "blocked", "asked_at": asked_at,
                "revision": ticket["revision"]}

    def _answer_ticket(self, conn, data):
        ticket = self._ticket(conn, data)
        qid, answer = text(data, "question_id", 200), text(data, "answer", 4000)
        answered_by = text(data, "answered_by", 20)
        require(answered_by in ("human", "coordinator"), "INVALID_INPUT", "answered_by must be human or coordinator")
        if answered_by == "human":
            # 代答凭据只在 coordinator 一侧存在；human 带了它，「谁答的」这一列就说不清。
            require("answered_by_session_id" not in data, "INVALID_INPUT",
                    "answered_by_session_id must be omitted when answered_by is human")
            answerer = None
        else:
            answerer = text(data, "answered_by_session_id", 500)
        prd_sha256 = digest_text(data, "prd_sha256")
        question = conn.execute("SELECT * FROM questions WHERE id=? AND ticket_id=?", (qid, ticket["id"])).fetchone()
        require(question is not None, "QUESTION_MISMATCH", "Question does not exist on this ticket")
        # 先看问题还开不开：被 recover / revise / cancel 作废的问题不收答案，晚到的那份答案已经过期。
        # 这里必须排在工作单状态之前：工单已经离开 blocked 时，「问题已作废」比「状态不对」更接近事实。
        require(question["state"] == "open", "QUESTION_MISMATCH",
                "Question is no longer open (answered or voided); an answer to it is stale")
        attempt = conn.execute("SELECT * FROM attempts WHERE id=?", (question["attempt_id"],)).fetchone()
        require(attempt is not None and attempt["id"] == ticket["current_attempt"]
                and attempt["revision"] == ticket["revision"],
                "STALE_ATTEMPT", "Question belongs to an attempt that is no longer current")
        require(prd_sha256 == ticket["prd_sha256"], "STALE_PRD",
                "PRD changed while the question was waiting; revise the ticket or ask again")
        # 兜底：一条 open 问题配着非 blocked 的工单。正常路径上两者总是成对变化，
        # 所以这条不会被正常路径碰到；它防的是库里出现过一次不一致行之后，答案把别的状态挟持回 running。
        require(ticket["state"] == "blocked", "INVALID_STATE", "Ticket is not waiting for an answer")
        if answered_by == "coordinator":
            require(answerer != attempt["session_id"], "SELF_ANSWER",
                    "The executing session cannot answer its own question")
            require(answerer == attempt["coordinator_session_id"], "NOT_COORDINATOR",
                    "Only the coordinator that dispatched this attempt may record an answer for a human")
        aid, created_at = str(uuid4()), now()
        conn.execute("INSERT INTO answers VALUES (?,?,?,?,?,?)", (aid, qid, answer, answered_by, answerer, created_at))
        conn.execute("UPDATE questions SET state='answered' WHERE id=?", (qid,))
        # 回到 running 只说明库里不再等答案，不代表那个会话已经在跑：唤醒靠一条新消息（4.2 / 4.5）。
        conn.execute("UPDATE attempts SET state='running' WHERE id=?", (attempt["id"],))
        conn.execute("UPDATE tickets SET state='running' WHERE id=?", (ticket["id"],))
        return {"answer_id": aid, "question_id": qid, "question_state": "answered",
                "ticket_state": "running", "attempt_id": attempt["id"],
                "answered_by": answered_by, "answered_at": created_at, "revision": ticket["revision"]}

    def _record_notice(self, conn, data):
        # 回信是整条派发链的收口：发没发出去、失败了为什么，都得在库里查得到（PRD-009 R5）。
        # 不在这里发任何消息，也不推动工单状态机：本操作只落库。
        ticket = self._ticket(conn, data)
        aid = text(data, "attempt_id", 200)
        attempt = conn.execute("SELECT * FROM attempts WHERE id=? AND ticket_id=?",
                               (aid, ticket["id"])).fetchone()
        require(attempt is not None, "STALE_ATTEMPT", "Attempt does not belong to this ticket")
        status = text(data, "status", 20)
        require(status in ("sent", "failed"), "INVALID_INPUT", "status must be sent or failed")
        origin = text(data, "origin_session_id", 500)
        detail = text(data, "detail", 4000)
        # 只由协调会话调：不是防伪（session_id 仍是自报字符串），是把「谁发的回执」
        # 与 claim 时写下的那一条对齐，执行会话不能替协调者宣告回信结果。
        require(text(data, "session_id", 500) == attempt["coordinator_session_id"], "NOT_COORDINATOR",
                "Only the coordinator that dispatched this attempt may record a notice")
        # 「发给谁」不能靠事后回想：投递目标必须就是 claim 时写下的那条协调会话。
        # 写成执行会话自己是最容易发生的错，那条回执根本到不了人手里。
        target = text(data, "target_session_id", 500)
        require(target == attempt["coordinator_session_id"], "NOT_TARGET",
                "target_session_id must be the coordinator session recorded at claim time")
        # 不属于白名单：宿主认得的值比本工具多，枚举外取值原样入库（见 wake_kind()）。
        # 只有 created 被特判，因为它是失败信号而不是另一种成功方式。
        wake = wake_kind(data)
        # created 的意思是宿主把它当成新建任务、没有投递到目标会话：那次回执没有到达任何人，
        # 所以一律记 failed，不因为调用方写了 sent 就变成一条成功记录。
        if wake == "created":
            status = "failed"
        if status == "sent":
            # 回信只发一次：第二次 sent 不是新事实，是重复宣告，留下第一条供人查。
            require(conn.execute("SELECT 1 FROM notices WHERE ticket_id=? AND status='sent'",
                                 (ticket["id"],)).fetchone() is None,
                    "ALREADY_NOTIFIED", "This ticket already has a sent notice; read it instead of writing another")
        nid = str(uuid4())
        conn.execute("INSERT INTO notices VALUES (?,?,?,?,?,?,?,?,?)",
                     (nid, ticket["id"], aid, origin, status, detail, now(), target, wake))
        # failed 可以多条（真会失败多次），它只留记录、不动工单状态：NOTIFY_FAILED 不推动任何状态机。
        result = {"notice_id": nid, "ticket_id": ticket["id"], "attempt_id": aid,
                  "origin_session_id": origin, "status": status, "detail": detail,
                  "target_session_id": target, "wake_kind": wake,
                  "alert": NOTIFY_FAILED if status == "failed" else None,
                  "ticket_state": ticket["state"], "revision": ticket["revision"]}
        if wake == "created":
            # 这条 failed 已经写进去（要事后查得到），但这次调用仍然失败：
            # 标记给 execute 汇总成非零退出的回执，见那里的 _reject 处理。
            result["alert"] = NOTIFY_CREATED_TASK
            result["_reject"] = {
                "code": NOTIFY_CREATED_TASK,
                "message": ("The host created a new task instead of delivering to the target session; "
                            "this notice is recorded as failed — stop that stray task and resend with an explicit "
                            "target_session_id"),
            }
        return result

    def _invalidate(self, conn, ticket, state):
        if ticket["current_attempt"]:
            # 只终止未完成的尝试；accepted/rejected/superseded 是审计真相，不得被覆盖。
            # blocked 必须在这张名单里，否则被作废的尝试会留着「还在等答案」的样子（PRD-005 4.4 第 3 条）。
            conn.execute("UPDATE attempts SET state='superseded' WHERE id=? AND state IN ('blocked','dispatching','running','submitted')",
                         (ticket["current_attempt"],))
            # 问题随之作废：作废之后晚到的答案只能报 QUESTION_MISMATCH（PRD-005 4.7）。
            # 放在这里而不是 _recover_ticket 里，是因为 revise / cancel 同样会把尝试作废。
            conn.execute("UPDATE questions SET state='voided' WHERE attempt_id=? AND state='open'",
                         (ticket["current_attempt"],))
        conn.execute("UPDATE tickets SET revision=revision+1,state=?,current_attempt=NULL WHERE id=?", (state, ticket["id"]))

    def _recover_ticket(self, conn, data):
        ticket = self._ticket(conn, data)
        self._expected_revision(ticket, data)
        reason = text(data, "reason")
        require(ticket["state"] in ("blocked", "dispatching", "running", "awaiting_review"), "INVALID_STATE",
                "Only an unfinished attempt may be explicitly recovered")
        require(ticket["current_attempt"] == text(data, "attempt_id", 200), "STALE_ATTEMPT",
                "Recovery must name the current attempt whose side effects were verified")
        superseded = ticket["current_attempt"]
        discarded = conn.execute("SELECT id FROM submissions WHERE attempt_id=?", (superseded,)).fetchone()
        # 回执里列出被作废的问题：它们保留在库里，但不再是「在等答案」的那一条。
        voided = [row["id"] for row in conn.execute(
            "SELECT id FROM questions WHERE attempt_id=? AND state='open' ORDER BY rowid", (superseded,))]
        self._invalidate(conn, ticket, "ready")
        return {"ticket_id": ticket["id"], "state": "ready", "revision": ticket["revision"] + 1, "reason": reason,
                "superseded_attempt_id": superseded,
                "discarded_submission_id": discarded["id"] if discarded else None,
                "voided_question_ids": voided}

    def _revise_ticket(self, conn, data):
        ticket = self._ticket(conn, data)
        self._expected_revision(ticket, data)
        require(ticket["state"] != "cancelled", "INVALID_STATE", "Cancelled tickets cannot be silently reopened")
        reopen = data.get("reopen", False)
        require(type(reopen) is bool, "INVALID_INPUT", "reopen must be a boolean")
        require(ticket["state"] != "done" or reopen, "INVALID_STATE",
                "Revising a delivered ticket reopens it; pass reopen: true to confirm")
        reason, checks = text(data, "reason"), criteria(data)
        deliverable = ticket["deliverable"]
        if "deliverable" in data:
            deliverable = text(data, "deliverable", 500)
        previous_state = ticket["state"]
        project = self._project(conn, ticket["project_id"])
        prd = fingerprint(project["root"], text(data, "prd_path"))
        self._invalidate(conn, ticket, "ready")
        conn.execute("UPDATE tickets SET prd_path=?,prd_sha256=?,criteria=?,deliverable=? WHERE id=?",
                     (prd["path"], prd["sha256"], packed(checks), deliverable, ticket["id"]))
        return {"ticket_id": ticket["id"], "state": "ready", "previous_state": previous_state,
                "revision": ticket["revision"] + 1, "reason": reason, "prd_path": prd["path"],
                "prd_sha256": prd["sha256"], "criteria": checks, "deliverable": deliverable}

    def _cancel_ticket(self, conn, data):
        ticket = self._ticket(conn, data)
        self._expected_revision(ticket, data)
        reason = text(data, "reason")
        require(ticket["state"] not in ("done", "cancelled"), "INVALID_STATE", "Ticket is already terminal")
        self._invalidate(conn, ticket, "cancelled")
        return {"ticket_id": ticket["id"], "state": "cancelled", "revision": ticket["revision"] + 1, "reason": reason}

    def _review_result(self, conn, data):
        ticket, attempt = self._active(conn, data)
        require(ticket["state"] == "awaiting_review", "INVALID_STATE", "Ticket is not awaiting review")
        project = self._unchanged_prd(conn, ticket)
        submission = conn.execute("SELECT * FROM submissions WHERE id=? AND attempt_id=?",
                                  (text(data, "submission_id", 200), attempt["id"])).fetchone()
        require(submission is not None, "SUBMISSION_MISMATCH", "Submission is not from this execution attempt")
        reviewer = text(data, "reviewer_session_id", 500)
        executed = conn.execute("SELECT 1 FROM attempts WHERE ticket_id=? AND session_id=?", (ticket["id"], reviewer)).fetchone()
        require(executed is None, "SELF_REVIEW", "A session that executed this ticket cannot review it")
        verdict, reason = text(data, "verdict", 20), text(data, "reason")
        require(verdict in ("accept", "reject"), "INVALID_INPUT", "Verdict must be accept or reject")
        checks = data.get("checks")
        expected_ids = {item["id"] for item in json.loads(ticket["criteria"])}
        require(isinstance(checks, list) and len(checks) == len(expected_ids), "INVALID_INPUT", "Review must cover every acceptance criterion exactly once")
        seen = set()
        for check in checks:
            require(isinstance(check, dict) and set(check) == {"id", "passed", "evidence"}, "INVALID_INPUT", "Check requires id, passed and evidence")
            cid = text(check, "id", 100)
            require(cid in expected_ids and cid not in seen and type(check["passed"]) is bool,
                    "INVALID_INPUT", "Unknown, duplicate or non-boolean review check")
            text(check, "evidence")
            seen.add(cid)
        all_passed = all(item["passed"] for item in checks)
        require((verdict == "accept") == all_passed, "INVALID_INPUT", "Accept requires all checks to pass; reject requires a failed check")
        for saved in json.loads(submission["artifacts"]):
            try:
                current = fingerprint(project["root"], saved["path"])
            except Problem as exc:
                require(False, "ARTIFACT_CHANGED",
                        f"Submitted artifact is no longer verifiable ({exc.code}); recover and resubmit before reviewing")
            require(current == saved, "ARTIFACT_CHANGED", "Submitted artifact changed; recover and resubmit before reviewing")
        review_id, state = str(uuid4()), "done" if verdict == "accept" else "rework"
        conn.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?,?)",
                     (review_id, submission["id"], reviewer, verdict, packed(checks), reason, now()))
        conn.execute("UPDATE tickets SET state=? WHERE id=?", (state, ticket["id"]))
        conn.execute("UPDATE attempts SET state=? WHERE id=?", ("accepted" if verdict == "accept" else "rejected", attempt["id"]))
        return {"review_id": review_id, "submission_id": submission["id"], "state": state, "checks": checks, "reason": reason}

    def _merge_ticket(self, conn, data):
        # 「验收通过」与「已经进了目标分支」是两件事：只有 done 工单才有资格写合并记录，
        # 同一张工单只允许一条，否则「它到底合到哪个 commit」会变成最后一次写入说了算。
        ticket = self._ticket(conn, data)
        require(ticket["state"] == "done", "INVALID_STATE", "Only an accepted (done) ticket can be merged")
        commit = text(data, "commit", 200)
        target_branch = text(data, "target_branch", 200)
        tests = text(data, "tests")
        existing = conn.execute("SELECT id FROM merges WHERE ticket_id=?", (ticket["id"],)).fetchone()
        require(existing is None, "ALREADY_MERGED", "This ticket already has a merge record; read it instead of writing another")
        # 没过回执的单合不进去：「验收了、合了、回执发没发不知道」正是这条链以前断掉的地方。
        # failed 也算走过——回执没能送达但原因记下来了，这是事实而不是可以拿来越过的门。
        require(conn.execute("SELECT 1 FROM notices WHERE ticket_id=? LIMIT 1", (ticket["id"],)).fetchone() is not None,
                "NO_NOTICE", "This ticket has no notice record; record a sent notice, or a failed one with its "
                             "reason, before merging")
        merge_id = str(uuid4())
        reviewed_sha = self._reviewed_sha(conn, ticket, data)
        conn.execute("INSERT INTO merges(id,ticket_id,commit_sha,target_branch,tests,created_at,reviewed_sha)"
                     " VALUES (?,?,?,?,?,?,?)",
                     (merge_id, ticket["id"], commit, target_branch, tests, now(), reviewed_sha))
        return {"merge_id": merge_id, "ticket_id": ticket["id"], "project_id": ticket["project_id"],
                "commit": commit, "target_branch": target_branch, "tests": tests,
                "reviewed_sha": reviewed_sha}

    def _reviewed_sha(self, conn, ticket, data):
        """合并时写下「被验收的那棵树」，并对得上才算数。

        三种情形各自不同，因为它们对应三种不同的事实：

        - 库里有 head_sha 且等于传进来的：那棵树就是被验收的，照写。
        - 库里有 head_sha 但不相等：验收之后又提交了，合进去的不是被验收的那棵树——
          报 `SHA_MISMATCH` 且一行不写。这正是这张单要堵的缝。
        - 库里是 NULL（schema 8 以前的老提交）：没有可比的值，所以不收任何 sha，也不许
          自称「核对过」——必须传字面量 `unverified`，并按这个字原样入库。老单因此既
          不会被静默放行，也不会被全堵死。
        """
        head_sha = latest_head_sha(conn, ticket["id"])
        value = text(data, "reviewed_sha", 40)
        if head_sha is None:
            # 传一个 sha 来也不能收：库里没有能和它比的东西，收了就是把「没核对」写成数值。
            require(value == UNVERIFIED, "INVALID_INPUT",
                    f"This ticket's newest submission has no head_sha (written before schema 9); "
                    f"pass the literal `{UNVERIFIED}` as reviewed_sha instead of a sha")
            return UNVERIFIED
        require(value != UNVERIFIED, "INVALID_INPUT",
                f"This ticket's submission recorded head_sha {head_sha}; `{UNVERIFIED}` would claim it was "
                "never checked")
        reviewed = sha_text(data, "reviewed_sha")
        require(reviewed == head_sha.lower(), "SHA_MISMATCH",
                f"The tree being merged was not the tree that was accepted: reviewed {value.lower()}, "
                f"latest submission {head_sha.lower()}. Commit on the branch was added after the review; "
                "revise the ticket and get it reviewed again, or merge the reviewed tree")
        return reviewed

    def _list_unmerged(self, conn, data):
        # 只读清单：已验收（done）但没有合并记录。历史工单直接在 master 上做、没有 merges 行，
        # 会照样出现在这里——这是事实，清单让人看见并判断，而不是替人隐去或补记录。
        pid = None
        if "project_id" in data:
            pid = text(data, "project_id", 200)
        rows = conn.execute(
            "SELECT t.id,t.project_id,t.title,t.state,t.revision,t.created_at FROM tickets t"
            " LEFT JOIN merges m ON m.ticket_id=t.id"
            " WHERE t.state='done' AND m.id IS NULL AND (? IS NULL OR t.project_id=?)"
            " ORDER BY t.created_at,t.id", (pid, pid))
        items = [{**dict(row), "has_merge": False} for row in rows]
        return {"items": items, "count": len(items)}

    def _events(self, conn, data):
        ticket = self._ticket(conn, data)
        limit, offset = integer(data, "limit", 50, 1), integer(data, "offset", 0, 0, 1000000)
        rows = conn.execute("SELECT * FROM events WHERE ticket_id=? ORDER BY id LIMIT ? OFFSET ?", (ticket["id"], limit, offset))
        items = [dict(row) for row in rows]
        for row in items:
            row["detail"] = json.loads(row["detail"])
        return {"items": items, "limit": limit, "offset": offset}
