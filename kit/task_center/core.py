"""Local trusted-user task commands. No host identity or session dispatch is implied."""
import hashlib
import json
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .artifacts import ARTIFACT_KEYS, artifact_drift, check_artifact, fingerprint, project_root
from .db import Problem, backup_database, connection, initialize, migrate_database, now, packed, require
from .sessions_context import message_id, sessions_for

# 空判据单的「已点名」台账（AW-140）：`events` 里 operation 等于本常量的行 = 这张单被
# `fix_pool` 点名过一次。写它的是 `record_pool_report`，读它的是 `tools/next_action.py`
# 的退避与升级判断——「报过几次」只有一个落库位置，不靠会话记忆。
POOL_REPORT = "record_pool_report"

FIELDS = {
    "init": set(),
    "migrate": set(),
    "backup": {"destination"},
    "review_result": {"project_id", "ticket_id", "attempt_id", "submission_id", "reviewer_session_id", "verdict", "checks", "reason"},
    "register_project": {"project_id", "name", "root"},
    "unregister_project": {"project_id"},
    # 换根不改身份（AW-154）：只动 `projects.root`，`project_id` / `name` / `next_number`
    # 与名下工单一律不变。有工单的项目以前改不了根（`unregister_project` 会拒），这条补上。
    "relocate_project": {"project_id", "new_root"},
    # 显示名可改，身份不动（AW-115）：`projects.name` 是给人看的名字，写错了得能改；
    # `changed_by` 是可选的执行会话标识（自报），只进事件与回执，不参与任何判定。
    "rename_project": {"project_id", "name", "changed_by"},
    "list_projects": set(),
    "create_ticket": {"project_id", "ticket_id", "title", "deliverable", "prd_path", "criteria", "root",
                      "state", "blocked_by", "related", "rides_with", "requester_session_id"},
    "get_ticket": {"project_id", "ticket_id"},
    "list_tickets": {"project_id", "limit", "offset", "planned", "--planned"},
    "events": {"project_id", "ticket_id", "limit", "offset"},
    "claim_ticket": {"project_id", "ticket_id", "expected_revision", "coordinator_session_id",
                     "coordinator_workdir", "allow_parallel", "parallel_reason", "parallel_scope"},
    "bind_session": {"project_id", "ticket_id", "attempt_id", "session_id", "project_root"},
    "submit_result": {"project_id", "ticket_id", "attempt_id", "session_id", "summary", "artifacts", "evidence", "risks",
                      "head_sha", "base_sha", "repo_kind"},
    "recover_ticket": {"project_id", "ticket_id", "attempt_id", "expected_revision", "reason"},
    "revise_ticket": {"project_id", "ticket_id", "expected_revision", "reason", "prd_path", "criteria", "deliverable", "reopen",
                      "restart", "blocked_by", "related", "rides_with"},
    "cancel_ticket": {"project_id", "ticket_id", "expected_revision", "reason"},
    # 第十个终态（AW-128）：只能由 `done` 进去，必带理由，`revision` 不变——归档不是
    # 新的一代，它是「这张单已经验收过、但账在库里对不上」这件事被承认下来。
    "archive_ticket": {"project_id", "ticket_id", "expected_revision", "reason"},
    "merge_ticket": {"project_id", "ticket_id", "commit", "target_branch", "tests", "reviewed_sha", "source", "note"},
    # 只改补记行的 `verified_by` 路径写法（反斜杠 → 正斜杠），命令本身不许变（AW-116）。
    "repair_backfill_command": {"project_id", "ticket_id"},
    "list_unmerged": {"project_id", "since_days"},
    "ask_ticket": {"project_id", "ticket_id", "attempt_id", "session_id", "question", "options"},
    "answer_ticket": {"project_id", "ticket_id", "question_id", "answer", "answered_by",
                      "answered_by_session_id", "prd_sha256"},
    "record_notice": {"project_id", "ticket_id", "attempt_id", "session_id", "origin_session_id",
                      "status", "detail", "target_session_id", "wake_kind", "message_id"},
    # 责任人接替（AW-114 C3）：`tickets.owner_session_id` 是「现在谁负责收尾」，
    # 与 `attempts.coordinator_session_id`（谁派的，不可变）分开。四个字段就是全部：
    # AW-114 的 C3 要求用这份 payload 就能跑通，所以不额外要求 expected_revision——
    # 接替不是新的一代，revision 不动，重复接替报 ALREADY_OWNER。
    "take_over_ticket": {"project_id", "ticket_id", "new_owner_session_id", "reason"},
    # 「验收已经派出去了」（AW-133）：只留一条 `review_dispatched` 事件，不改工单状态、
    # 不碰 `reviews`。派单器读到它就不再对同一张单派第二个验收会话，30 分钟没有
    # `reviews` 行自动回落成可派（窗口常量见 `REVIEW_IN_FLIGHT_SECONDS`）。
    "review_dispatched": {"project_id", "ticket_id", "submission_id",
                          "reviewer_session_id", "note"},
    # 空判据单「已经点名报过」（AW-140）：一条事件，不改状态，读侧靠它数轮次、退避、升级。
    POOL_REPORT: {"project_id", "ticket_id", "session_id", "note"},
    # 执行会话记自己那次没送到的回执（AW-114 C2）：只收 failed，没有 status 字段。
    "report_notice_failure": {"project_id", "ticket_id", "attempt_id", "session_id",
                              "target_session_id", "detail", "wake_kind"},
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

# 验收派出去之后压住同一张单的时长（AW-133）：超过这么久还没有 `reviews` 行，
# `tools/next_action.py` 就把这张单重新算成可派——验收会话可能根本起没来，或宿主把它
# 当成了别的事。窗口只有一个来源：本常量，派单侧与读侧都不许各写一个 30。
REVIEW_IN_FLIGHT_SECONDS = 30 * 60
# 尝试级的「在飞」：submitted 对应 awaiting_review，同样算「还在这一代里工作」。
IN_FLIGHT_ATTEMPT_STATES = ("blocked", "dispatching", "running", "submitted")

# 状态名。`planned` 是「还没开工、在库里排着」（PRD-014 R1）；它不占 active_session、
# 不产生尝试，所以 ACTIVE_TICKET_STATES 里没有它，`DISPATCH_BUSY` 也不因它而报。
PLANNED_STATE = "planned"
# 第十个终态（AW-128）：验收通过（`done`）之后，账却对不上的那批存量单被承认为历史事实。
# 它只由 `archive_ticket` 从 `done` 进入（取消是 `cancelled`，两回事：取消=没做完，
# 归档=做完了但证据链断在 schema 8 之前）。进去之后不进 `list_unmerged`、不被
# `wait_audit` 的 done 两类命中、不算在途，但 `reviews` / `submissions` / 历史行一条不删——
# 「它验收过」这个事实不许因为清单好看而被抹掉。所以它也不在 ACTIVE_TICKET_STATES 里。
ARCHIVED_STATE = "archived"
# create_ticket 只发这两种状态；其余状态只能由状态机走出来，不能建单时直接声称。
CREATABLE_STATES = (PLANNED_STATE, "ready")
# claim 收这三种：`planned` 与 `ready` 是「还没开工」，`rework` 是「改完再来」。
CLAIMABLE_STATES = (PLANNED_STATE, "ready", "rework")
# 依赖边的三种 kind（PRD-014 R3）：只有 blocked_by 拦 claim。
LINK_KINDS = ("blocked_by", "related", "rides_with")

# `list_tickets` 的输出表头（PRD-013 R7）。写死成一份列单而不是从行里推：
# 表头要能回答「这里该有哪几列」，哪怕这一次一张单也没有。
LIST_COLUMNS = ("id", "project_id", "title", "state", "revision", "criteria_v", "behind", "created_at")
# planned 视图多出的三列（PRD-014 R6.2）：只在这一个视图里出现，其余视图的表头与以前逐字一致。
# `criteria_n` 是 AW-109 加的：池子里要分得清「念头」（0 条条件）与「写好的单」。
PLANNED_COLUMNS = ("claimable", "blocked_by", "criteria_n")
# 条件可以为空的唯一状态（PRD-014 R1 的放宽 / AW-109）：`planned` 是「知道要补什么、
# 还写不出验收条件」的那个位置。它不产生尝试、不算在途、也领不走（`claim_ticket`
# 读到空条件就报 CRITERIA_NOT_JUDGEABLE），所以放宽入口不等于降低开工标准。
EMPTY_CRITERIA_STATES = (PLANNED_STATE,)
# `behind` 的基准分支：R7 写死了 `master`，不按仓库的主分支名字猜。
BEHIND_TARGET = "master"


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


# 条件形状闸门的两种合法写法（PRD-013 R1）：引号内的命令动词在这张名单里，
# 或者文本自称「判断类」并写出反例。名单收的是本仓库和本机真会跑的那几个，
# 不是所有命令——收得越宽，闸门越像在空转。
COMMAND_VERBS = ("python", "git", "grep", "ls", "find", "wc", "node", "npm", "bash")
JUDGEMENT_CLASS = "判断类"
COUNTEREXAMPLE = "反例"
BACKTICKED = re.compile(r"`([^`]+)`")
# 命令位置：反引号里可能是 `cd X && git status` 这种复合命令，按 shell 分隔符切开看每一段。
COMMAND_BREAK = re.compile(r"&&|\|\||[;|\n]")
WORD = re.compile(r"[A-Za-z]+")


def quoted_command(value):
    """文本里第一处「引号包裹、动词在名单内」的命令；没有就返回 None。"""
    for fragment in BACKTICKED.finditer(value):
        for part in COMMAND_BREAK.split(fragment.group(1)):
            tokens = part.split()
            if not tokens:
                continue
            verb = WORD.match(tokens[0])
            if verb and verb.group(0).lower() in COMMAND_VERBS:
                return fragment.group(0)
    return None


def judgeable(cid, value):
    """R1：条件只收两种写法，都不满足就点名拒掉，一行不写。

    它不判「这条条件合理吗」——那件事机器判不了（PRD-013 §2）。这里只判
    「可执行」或「你已经明说这是判断类并给了反例」，也就是把写在 skill 里的口径
    变成一次当场失败。
    """
    if quoted_command(value):
        return
    if value.strip().startswith(JUDGEMENT_CLASS) and COUNTEREXAMPLE in value:
        return
    raise Problem("CRITERIA_NOT_JUDGEABLE",
                  f"Criterion {cid} is not judgeable: quote a command whose verb is one of "
                  f"{'/'.join(COMMAND_VERBS)} (for example `python tools/run_tests.py`) with its expected output, "
                  f"or start the text with {JUDGEMENT_CLASS} and include {COUNTEREXAMPLE}; nothing was written")


def criteria(data, state=None):
    """条件列表：`planned` 可以一条都没有，其余状态至少一条（AW-109）。

    `state` 不传时按「必须有条件」判：调用方不愿意说自己在哪个状态，就不给它放宽的
    那一半——空列表在没有状态的上下文里永远是一个错误。
    """
    value = data.get("criteria", [])
    require(isinstance(value, list) and len(value) <= 100, "INVALID_INPUT", "Provide at most 100 criteria")
    if not value:
        if state in EMPTY_CRITERIA_STATES:
            return []
        # 「一条都没有」不是形状错，是这条工单还不可以被验收：用同一个错误码说同一件事。
        raise Problem("CRITERIA_NOT_JUDGEABLE",
                      "A ticket needs at least one judgeable criterion before it can leave the pool; "
                      f"only {PLANNED_STATE} may keep an empty list. Quote a command whose verb is one of "
                      f"{'/'.join(COMMAND_VERBS)} with its expected output, or start the text with "
                      f"{JUDGEMENT_CLASS} and include {COUNTEREXAMPLE}; nothing was written")
    ids = set()
    for item in value:
        require(isinstance(item, dict) and set(item) == {"id", "text"}, "INVALID_INPUT", "Criterion requires id and text")
        cid = text(item, "id", 100)
        text(item, "text")
        require(cid not in ids, "INVALID_INPUT", "Criterion ids must be unique")
        ids.add(cid)
    # 形状单独走第二遍：结构错误（缺字段、空文本、id 重复）先报完，才不会把
    # 「字段写错了」报成「条件写不成样子」——那个错误码只说一件事。
    # 只判这次传进来的值：库里已有的行不重判（PRD-013 R1、D1）。
    for item in value:
        judgeable(item["id"], item["text"])
    return value


def cancelled_message(ticket, action):
    """取消之后为什么不能接着做：一句话说清事实、后果与唯一出路（AW-159）。

    为什么值得单独写一句而不是复用「状态不对」：`cancel_ticket` 把 `current_attempt`
    置空、`revision` 加一，于是取消后的调用最先撞上的往往是 `STALE_ATTEMPT` /
    `STALE_REVISION`——那两句话的意思是「重读一遍再来」，而这条路在所有状态下都走不通。
    2026-11-24 HSE-005 现场就是这么走到「手改库」那一步的：14:04:57 取消，14:28:38
    库里凭空多出一条 attempt，`events` 里没有对应的 claim。所以第一句要说的是
    「这张单已经取消」，它是唯一一条让重试失去意义的事实。
    """
    return (f"{ticket['id']} 已经取消（state=cancelled, revision={ticket['revision']}）："
            f"{action} 不接受，一行都没有写。取消是单向门——之后不再产生新尝试、不推高 "
            "revision、不收执行结果、也不能被恢复或重开。要把这件事接着做，新建一张单"
            "（create_ticket）；取消原因在 events 里最后一条成功的 cancel_ticket 的 result.reason。")


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


def ticket_prefix(project_id):
    """工单号的前缀：取项目登记的短名（`ai-workspace` → `AW`，`p1` → `P1`）。

    多段时取每段首字母拼起来，单段时用整段：`ai-workspace` 因此得到 AW 而不是 A，
    而 `p1` / `p2` 各自留下自己的字母，不会一起退化成 P。全非拉丁的 project_id
    没有字母可拼，退回首两个字——号仍然可读，只是没缩写。
    """
    parts = [part for part in re.split(r"[^0-9A-Za-z]+", project_id) if part]
    if len(parts) > 1:
        return "".join(part[0] for part in parts).upper()
    if parts:
        return parts[0].upper()
    return project_id[:2].upper()


def link_payload(data, maximum=50):
    """从 payload 取三种依赖边（PRD-014 R3）：只收本操作里出现过的 kind。

    没出现的 kind 一律不动（revise 只改调用方说的那一类），出现了就按给的那份覆盖。
    """
    links = {}
    for kind in LINK_KINDS:
        if kind not in data:
            continue
        value = data[kind]
        require(isinstance(value, list) and len(value) <= maximum, "INVALID_INPUT",
                f"{kind} must be an array of at most {maximum} ticket ids")
        for item in value:
            text({kind: item}, kind, 200)
        links[kind] = list(value)
    return links


def pending_blockers(conn, ticket_id):
    """未完成的前置单：`done` 与 `archived` 算满足，`cancelled` 也算未满足（PRD-014 R3.4）。

    `archived` 在这里算满足，是因为它只能从 `done` 进来（AW-128）：归档承认的是
    「做过、验收过、只是账对不上」，不是「没做完」。把归档单当未完成，会让
    「B 卡在已归档的 A 上」永远领不走——那正是取消要表达的意思，两种终态不能混。
    """
    return conn.execute(
        "SELECT t.id, t.state FROM ticket_links l JOIN tickets t ON t.id=l.to_ticket"
        " WHERE l.from_ticket=? AND l.kind='blocked_by' AND t.state NOT IN ('done', 'archived')"
        " ORDER BY t.id",
        (ticket_id,)).fetchall()


def dependency_depths(conn, ticket_ids):
    """每张单的 blocked_by 层数：无前置为 0，其余为 1 + 前置里最深的那个。

    层级按边算，不按前置单的状态算：前置已经 done 之后，「B 在 A 之前」这条顺序
    依旧成立，池子不会因为一张单完成就重新洗牌。环在写入那一刻就被拒（DEPENDENCY_CYCLE），
    这里仍然防一手——遇到环就停在当前层，不把一次 list 查询挂死。
    """
    graph = {}
    for row in conn.execute("SELECT from_ticket,to_ticket FROM ticket_links WHERE kind='blocked_by'"):
        graph.setdefault(row[0], []).append(row[1])
    memo, visiting = {}, set()

    def depth(ticket_id):
        if ticket_id in memo:
            return memo[ticket_id]
        if ticket_id in visiting:
            return 0
        visiting.add(ticket_id)
        value = 0
        for prerequisite in graph.get(ticket_id, ()):
            value = max(value, depth(prerequisite) + 1)
        visiting.discard(ticket_id)
        memo[ticket_id] = value
        return value

    return {ticket_id: depth(ticket_id) for ticket_id in ticket_ids}


def dependency_cycle(conn):
    """blocked_by 图上的一个环（按行走顺序，首尾同一个单），没有环返回 None。"""
    graph = {}
    for row in conn.execute("SELECT from_ticket,to_ticket FROM ticket_links"
                            " WHERE kind='blocked_by' ORDER BY from_ticket,to_ticket"):
        graph.setdefault(row[0], []).append(row[1])
    state, stack = {}, []

    def walk(node):
        state[node] = 1
        stack.append(node)
        for following in graph.get(node, ()):
            if state.get(following) == 1:
                return stack[stack.index(following):] + [following]
            if state.get(following) is None:
                found = walk(following)
                if found is not None:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for node in list(graph):
        if state.get(node) is None:
            found = walk(node)
            if found is not None:
                return found
    return None


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


def behind_count(root, head_sha, cache=None):
    """目标分支上有、这份提交里没有的提交数；算不出来时是 `?`，不是 0（PRD-013 R7）。

    为什么不能写 0：`head_sha` 为 NULL 的历史提交、分支已被删、目录不是仓库这三种
    情况都算不了，而 0 与「基于最新 master」在输出里长得一模一样——那个 0 会变成一句假话。
    所以只有 `git rev-list --count` 真的给出一个数字时才写数字。
    """
    if not head_sha:
        return "?"
    key = (str(root), head_sha)
    if cache is not None and key in cache:
        return cache[key]
    value = "?"
    try:
        process = subprocess.run(
            ["git", "-C", str(root), "rev-list", "--count", f"{head_sha}..{BEHIND_TARGET}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
        raw = (process.stdout or "").strip()
        if process.returncode == 0 and raw.isdigit():
            value = int(raw)
    except (OSError, subprocess.SubprocessError):
        # git 不在、超时、目录不可达：都是「算不出来」，统一留 ?。
        value = "?"
    if cache is not None:
        cache[key] = value
    return value


# 冲突预演的目标分支：与 `behind` 同一口径，同样写死 `master`，不按仓库主分支名字猜（PRD-015 R2）。
CONFLICT_TARGET = BEHIND_TARGET


def conflict_preview(root, head_sha):
    """提交时对未来的目标分支跑一次 `git merge-tree`，只报「会撞哪些文件」（PRD-015 R2）。

    预演问的是「这份成果将来合进**现在的**目标分支会撞什么」，所以目标取 `rev-parse master`
    的当前 tip，不是 `base_sha`——用分叉点问等于没问。

    三种取值必须分得开：`conflicts` 空数组＝跑过且干净；非空且 `exit_code` 为 1＝跑过且会撞；
    整个返回 `None`＝算不出来（目录不是仓库、head 不在本地、git 不可用），**不是**「没撞」。
    把它写成空值等于说「预演过了、没冲突」，那是一句假话。

    这里只读不写：不 rebase、不改分支、不因为会撞就让提交失败——提交那一刻撞不撞不等于最终
    撞不撞，做成闸门只会教人先 rebase 一次再提交，回执里就再也看不到真相。
    """
    try:
        target = subprocess.run(
            ["git", "-C", str(root), "rev-parse", CONFLICT_TARGET],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
        target_sha = (target.stdout or "").strip()
        if target.returncode != 0 or not target_sha:
            return None
        # 输出读法（git 2.53 实测）：第一行是这次合并的树对象；其后到第一个空行为止的每一行
        # 是一个冲突文件路径；空行之后的 `Auto-merging …` / `CONFLICT (content): …` 是给人读的说明，
        # 不进字段。无冲突时那一段为空，退出码 1 表示有冲突、0 表示干净。
        process = subprocess.run(
            ["git", "-C", str(root), "merge-tree", "--write-tree", "--name-only", head_sha, CONFLICT_TARGET],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        if process.returncode not in (0, 1):
            return None
        lines = (process.stdout or "").splitlines()
        # 第一行必须是树对象 id：认不出来的就不算一次预演。实测 git 2.53 在「head 不是本地对象」时
        # 把 `merge-tree: <sha> - not something we can merge` 写在 stdout 上、退出码也是 1——
        # 照退出码读会得到「跑过、无冲突」的空数组，正好是这一列最不能说的假话。
        if not lines or not re.fullmatch("[0-9a-f]{40}", lines[0].strip()):
            return None
        paths = []
        for line in lines[1:]:
            if not line.strip():
                break
            paths.append(line.strip())
        return {"target_branch": CONFLICT_TARGET, "target_sha": target_sha,
                "command": f"git merge-tree --write-tree --name-only {head_sha} {CONFLICT_TARGET}",
                "exit_code": process.returncode, "conflicts": sorted(set(paths))}
    except (OSError, subprocess.SubprocessError):
        # git 不在、超时、目录不可达：都是「算不出来」，统一返回 None（落库为 NULL）。
        return None


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


def ticket_dict(row, warnings=None):
    value = dict(row)
    value["criteria"] = json_column(value["criteria"], "criteria", f"ticket {value.get('id')}", warnings, as_list=True)
    return value


def ancestry_proof(root, sha, branch):
    r"""能原样重跑的那条核对命令。

    路径统一用正斜杠：库里登记的 Windows 路径是 `d:\...`，抄进 Git Bash 时反斜杠会被
    当转义吃掉（`d:\cindy` 变成 `d:cindy`），命令当场跑不通，而 `verified_by` 的全部价值
    就是「照抄一遍就能复核」。正斜杠在 bash / PowerShell / cmd 三种壳里都能用。
    """
    shown = str(root).replace("\\", "/")
    if " " in shown:
        shown = f'"{shown}"'
    return f"git -C {shown} merge-base --is-ancestor {sha} {branch}"


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
                    # 建单的号在这一刻才存在：payload 里没有 ticket_id（自报号已被拒），
                    # 事件只能挂在回执刚发出去的那个号上，否则「这张单什么时候建的」
                    # 在 events 里查不到——ticket_id 为 NULL 的行无法按单查询。
                    event_ticket = payload.get("ticket_id")
                    if event_ticket is None and operation == "create_ticket" and isinstance(result, dict):
                        event_ticket = result.get("id")
                    conn.execute("INSERT INTO events(ticket_id,operation,detail,created_at) VALUES (?,?,?,?)",
                                 (event_ticket, operation, packed({"request_id": request_id, "result": result}), now()))
                return response
        except Problem as exc:
            self._record_rejection(operation, payload, request_id, exc.code)
            return {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        except sqlite3.IntegrityError:
            self._record_rejection(operation, payload, request_id, "CONFLICT")
            return {"ok": False, "error": {"code": "CONFLICT", "message": "Conflicting project, ticket, session or result"}}
        except sqlite3.Error as exc:
            return {"ok": False, "error": {"code": "STORAGE_ERROR", "message": str(exc)}}
        except UnicodeEncodeError:
            return {"ok": False, "error": {"code": "INVALID_INPUT",
                                           "message": "Text cannot be encoded as UTF-8; write the JSON input as UTF-8 (ASCII-escaped JSON is safest)"}}
        except (ValueError, TypeError, OSError, RecursionError) as exc:
            return {"ok": False, "error": {"code": "INVALID_INPUT", "message": unreadable_message(operation, exc)}}

    def _record_rejection(self, operation, payload, request_id, code):
        """R3：被拒的写操作留一行事件，别的什么都不写。

        为什么单独开一次事务：被拒的那一刻原来那张事务已经回滚了（`connection` 在异常时
        rollback），工单、尝试、requests 都不能落，但「这次被闸门拦下了」这件事必须留下。
        这是**只读的事实**，不改任何状态，也不产生幂等记录——用同一编号重试仍会重新判定。

        `detail` 只放错误码与请求编号：payload 里可能有口令、token、摘要一类内容，
        拒绝行是要长住库里的，不能把整份输入抄进去。`request_id` 缺失或与既有记录
        冲突（`REQUEST_CONFLICT` 说明这个编号已经属于另一次调用）时写 null。
        """
        if not isinstance(operation, str) or operation in READS:
            return
        try:
            with connection(self.path, write=True) as conn:
                ticket_id = payload.get("ticket_id") if isinstance(payload, dict) else None
                # events.ticket_id 有外键：拒绝可能发生在工单本身就不存在的时候（例如
                # create_ticket 被条件闸门拦下），那种行写的是 NULL——「没有这张单」也是事实。
                if not isinstance(ticket_id, str) or conn.execute(
                        "SELECT 1 FROM tickets WHERE id=?", (ticket_id,)).fetchone() is None:
                    ticket_id = None
                kept = None
                if (code != "REQUEST_CONFLICT" and isinstance(request_id, str)
                        and bool(request_id.strip()) and len(request_id) <= 200):
                    kept = request_id
                conn.execute("INSERT INTO events(ticket_id,operation,detail,created_at) VALUES (?,?,?,?)",
                             (ticket_id, operation, packed({"rejected": code, "request_id": kept}), now()))
        except Exception:
            # 留痕本身失败不能让原错误变成另一个错误：拒收的原因已经要返回给调用方了，
            # 而库没初始化、schema 不对这几种现场本来就没地方写事件。
            pass

    def _project(self, conn, pid):
        row = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        require(row is not None, "NOT_FOUND", "Project does not exist")
        return row

    def _ticket(self, conn, data):
        row = conn.execute("SELECT * FROM tickets WHERE id=?", (text(data, "ticket_id", 200),)).fetchone()
        require(row is not None, "NOT_FOUND", "Ticket does not exist")
        require(row["project_id"] == text(data, "project_id", 200), "PROJECT_MISMATCH", "Ticket belongs to a different project")
        self._batch_of_ticket(conn, row)
        return row

    def _batch_of_ticket(self, conn, ticket):
        """车次与工单必须同项目（PRD-014 R4.1）：一个车次＝一次资源占用窗口，它只在一个项目里。

        外键只保证「车次存在」，说不出「它在同一个项目」——跨项目的 `tickets.batch_id`
        是一条能落进库、却没有任何语义的行。本版没有建车次、也没有搭车的操作（见
        `docs/protocol.md` 的「车次」一节），所以判据放在按号取单的唯一入口 `_ticket`：
        脏行从这里进来之后，任何操作都拿不到它，也不必在每个操作里各判一次。
        """
        if ticket["batch_id"] is None:
            return None
        batch = conn.execute("SELECT * FROM batches WHERE id=?", (ticket["batch_id"],)).fetchone()
        if batch is None:
            # 缺失的车次由 `claim_ticket` 那条脏行判据（INVALID_STATE）回答，两处不重复报。
            return None
        require(batch["project_id"] == ticket["project_id"], "BATCH_PROJECT_MISMATCH",
                f"Ticket {ticket['id']} of project {ticket['project_id']} names batch {batch['id']} "
                f"of project {batch['project_id']}; a trip belongs to one project, so the row cannot be used "
                "until the batch is fixed")
        return batch

    def _register_project(self, conn, data):
        pid, name = text(data, "project_id", 200), text(data, "name", 200)
        root = project_root(data.get("root"))
        # root 是身份、project_id 是标签：同一路径重复登记复用原记录，不改已有标签。
        # 这里不报 CONFLICT，也不按新标签改名——名字写错了用 `rename_project` 改（只动
        # `name`），不必撤销重建；有工单的项目本来也撤不掉（`unregister_project` 会拒），
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
        conn.execute("INSERT INTO projects(id,name,root) VALUES (?,?,?)", (pid, name, root))
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

    def _rename_project(self, conn, data):
        """只改显示名，身份（id / root / next_number）与工单归属一律不动。

        起因是名字写错之后没有正当途径改：`unregister_project` 对有工单的项目直接报
        `PROJECT_HAS_TICKETS`（cindy-harmony 名下 73 张，2026-11-22 按
        `select count(*) from tickets where project_id='cindy-harmony'` 核），而进度页读的
        就是这一列，写错了就永远显示错。所以这里只做一件事：把 `projects.name` 换成新的。

        三种非法输入都在写入之前拒，被拒时一行不写：空名（含只有空白）与超长由 `text()`
        判 INVALID_INPUT，不存在的 project_id 由 `_project` 判 NOT_FOUND。
        旧名与新名都进回执，落库的事交给统一出口——它按 `operation='rename_project'`
        写一条事件，detail 里就是这份回执，所以「谁把哪个名字改成了什么」查得到。
        """
        project = self._project(conn, text(data, "project_id", 200))
        name = text(data, "name", 200)
        changed_by = text(data, "changed_by", 500) if "changed_by" in data else None
        conn.execute("UPDATE projects SET name=? WHERE id=?", (name, project["id"]))
        return {"project_id": project["id"], "old_name": project["name"], "name": name,
                "root": project["root"], "changed_by": changed_by}

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
        conn.execute("INSERT INTO projects(id,name,root) VALUES (?,?,?)", (pid, name, root))
        return root

    def _create_ticket(self, conn, data):
        # 号由库里发（PRD-014 R5）：回执里的 ticket_id 才是真号，自报一律拒。
        require("ticket_id" not in data, "INVALID_INPUT",
                "号由库里发：不接受调用方指定 ticket_id，以回执里的 ticket_id 为准")
        pid = text(data, "project_id", 200)
        root = self._ticket_root(conn, pid, data.get("root"))
        # 状态先解析、再判条件：只有 `planned` 允许空着条件，所以闸门要知道这张单进哪个状态。
        state = text(data, "state", 20) if "state" in data else "ready"
        require(state in CREATABLE_STATES, "INVALID_INPUT",
                "state must be planned or ready; 其余状态只能由状态机走出来，不能建单时写死")
        title, checks = text(data, "title", 500), criteria(data, state)
        # 交付物主题不是可选备注：只约束验收条件、不约束成果主题时，成果会合规地跑偏。
        deliverable = text(data, "deliverable", 500)
        # 只校 PRD 路径存在，不算摘要也不存它（PRD-014 R2.1）：依据在 claim 那一刻才定格。
        # fingerprint() 顺带把「文件不在项目里」「超过 32 MiB」这几件事当场判掉。
        prd = fingerprint(root, text(data, "prd_path"))
        links = link_payload(data)
        # 请求方是发起这张单的会话（PRD-015 R5）：可选，不传就留 NULL。
        # 关键是不拿协调会话冒充：两者大多数时候不是同一条会话，写错了就是一条假事实。
        requester = text(data, "requester_session_id", 500) if "requester_session_id" in data else None
        tid = self._next_ticket_id(conn, pid)
        self._check_link_targets(conn, tid, links)
        # 列名写全，不用位置插入：schema 每加一列，位置插入就会静默错位。
        conn.execute("INSERT INTO tickets(id,project_id,title,deliverable,prd_path,prd_sha256,criteria,"
                     "revision,criteria_version,state,current_attempt,created_at,requester_session_id)"
                     " VALUES (?,?,?,?,?,NULL,?,1,1,?,NULL,?,?)",
                     (tid, pid, title, deliverable, prd["path"], packed(checks), state, now(), requester))
        self._write_links(conn, tid, links)
        return ticket_dict(self._ticket(conn, {"project_id": pid, "ticket_id": tid}))

    def _next_ticket_id(self, conn, project_id):
        """发号：号在写 `tickets.id` 那一刻发出，它同时是主键（PRD-014 R5.1）。

        每个项目一条独立序列（`projects.next_number`），在同一个事务里取号并加一。
        号是全库主键，所以还要跳过已经被占的候选值——两个项目算出同一个前缀时，
        后者往后顺延，而不是撞上一个别人的单（那会让两个项目共用一个号）。
        """
        prefix = ticket_prefix(project_id)
        row = conn.execute("SELECT next_number FROM projects WHERE id=?", (project_id,)).fetchone()
        number = row["next_number"] if row is not None else 1
        while conn.execute("SELECT 1 FROM tickets WHERE id=?",
                           (f"{prefix}-{number:03d}",)).fetchone() is not None:
            number += 1
        conn.execute("UPDATE projects SET next_number=? WHERE id=?", (number + 1, project_id))
        return f"{prefix}-{number:03d}"

    def _check_link_targets(self, conn, from_ticket, links):
        for kind, targets in links.items():
            for target in targets:
                require(target != from_ticket, "INVALID_INPUT",
                        f"A ticket cannot be linked to itself ({kind})")
                require(conn.execute("SELECT 1 FROM tickets WHERE id=?", (target,)).fetchone() is not None,
                        "NOT_FOUND", f"{kind} names {target}, which is not a ticket in this center")

    def _write_links(self, conn, from_ticket, links):
        """按 kind 覆盖式写入：出现过的 kind 先清后写，没出现的 kind 不动。

        环检测放在写完边之后、同一条事务里：成环就抛 DEPENDENCY_CYCLE，整个事务回滚，
        连刚建的那张单、刚改的判据一起不落库（PRD-014 R3.3）。比对「只查新加的那条边」
        更彻底：一次调用里写进去的多条边之间的环也跑不掉。
        """
        for kind, targets in links.items():
            conn.execute("DELETE FROM ticket_links WHERE from_ticket=? AND kind=?", (from_ticket, kind))
            for target in dict.fromkeys(targets):
                conn.execute("INSERT INTO ticket_links(from_ticket,kind,to_ticket,created_at) VALUES (?,?,?,?)",
                             (from_ticket, kind, target, now()))
        if "blocked_by" in links:
            cycle = dependency_cycle(conn)
            require(cycle is None, "DEPENDENCY_CYCLE",
                    "依赖成环，一行未写：" + " -> ".join(cycle or [])
                    + "；环上的单都无法开工，所以要么改成单向，要么拆开")

    def _get_ticket(self, conn, data):
        warnings: list = []
        result = ticket_dict(self._ticket(conn, data), warnings)
        result["attempts"] = [dict(row) for row in conn.execute("SELECT * FROM attempts WHERE ticket_id=? ORDER BY rowid", (result["id"],))]
        submissions = conn.execute("SELECT s.* FROM submissions s JOIN attempts a ON a.id=s.attempt_id WHERE a.ticket_id=? ORDER BY s.rowid", (result["id"],))
        result["submissions"] = [dict(row) for row in submissions]
        for item in result["submissions"]:
            # 预演原样回给验收会话：NULL 仍然是 None（「没跑过／算不出来」），不是
            # `{"conflicts": []}`（「跑过且干净」）——两者在验收眼里是两件事（PRD-015 R2）。
            decode_submission(item, warnings)
        reviews = conn.execute("SELECT r.* FROM reviews r JOIN submissions s ON s.id=r.submission_id JOIN attempts a ON a.id=s.attempt_id WHERE a.ticket_id=? ORDER BY r.rowid", (result["id"],))
        result["reviews"] = [dict(row, **review_snapshot(row["checks"], row["verdict"], warnings, f"review {row['id']}")) for row in reviews]
        latest = result["reviews"][-1] if result["reviews"] else None
        result["review_summary"], result["unverified_checks"] = (latest["summary"], latest["checks_summary"]["unverified"]) if latest else (None, 0)
        result["merges"] = [dict(row) for row in conn.execute(
            "SELECT * FROM merges WHERE ticket_id=? ORDER BY rowid", (result["id"],))]
        # 问答随尝试挂在工单下面：验收会话靠它判断「成果是不是按最终结论做的」（PRD-005 3）。
        result["questions"] = [dict(row) for row in conn.execute(
            "SELECT * FROM questions WHERE ticket_id=? ORDER BY rowid", (result["id"],))]
        for item in result["questions"]:
            item["options"] = json_column(item["options"], "options", f"question {item['id']}", warnings, as_list=True)
        result["answers"] = [dict(row) for row in conn.execute(
            "SELECT a.* FROM answers a JOIN questions q ON q.id=a.question_id WHERE q.ticket_id=? ORDER BY a.rowid",
            (result["id"],))]
        # 回信记录随工单挂上来：发了没、失败原因、发给谁，验收会话读 get_ticket 就能判闭环（PRD-009 AC06）。
        result["notices"] = [dict(row) for row in conn.execute(
            "SELECT * FROM notices WHERE ticket_id=? ORDER BY rowid", (result["id"],))]
        # 一张单牵扯到哪些会话、哪条消息：开工即看得见，不用人把 session id 抄进派发消息（PRD-015 R5）。
        # 只答库里真有的值：空标识不产出条目，device 取不到就是 null，归档/删除标 stale。
        result["sessions"] = sessions_for(conn, result["id"])
        # 依赖边两个方向都答得出（PRD-014 R3.2）：出边说「它卡在谁身上」，入边说「谁在等它」。
        # 每边带对方的当前状态：只看单号看不出上游已经 done 还是还在 planned。
        result["links"] = {
            "out": [{"kind": row["kind"], "ticket_id": row["to_ticket"], "state": row["state"]}
                    for row in conn.execute(
                        "SELECT l.kind,l.to_ticket,t.state FROM ticket_links l"
                        " LEFT JOIN tickets t ON t.id=l.to_ticket"
                        " WHERE l.from_ticket=? ORDER BY l.kind,l.to_ticket", (result["id"],))],
            "in": [{"kind": row["kind"], "ticket_id": row["from_ticket"], "state": row["state"]}
                   for row in conn.execute(
                       "SELECT l.kind,l.from_ticket,t.state FROM ticket_links l"
                       " LEFT JOIN tickets t ON t.id=l.from_ticket"
                       " WHERE l.to_ticket=? ORDER BY l.kind,l.from_ticket", (result["id"],))],
        }
        if warnings:
            result["decode_warnings"] = warnings
        return result

    def _list_tickets(self, conn, data):
        limit, offset = integer(data, "limit", 50, 1), integer(data, "offset", 0, 0, 1000000)
        pid = data.get("project_id")
        if pid is not None:
            text(data, "project_id", 200)
        # CLI 的 `--planned` 与 payload 里的 `planned` 同一件事：两种写法收同一个布尔。
        planned = data.get("planned", data.get("--planned", False))
        require(type(planned) is bool, "INVALID_INPUT", "planned must be a boolean")
        if planned:
            return self._planned_view(conn, pid, limit, offset)
        rows = conn.execute("SELECT * FROM tickets WHERE (? IS NULL OR project_id=?) ORDER BY created_at,id LIMIT ? OFFSET ?",
                            (pid, pid, limit, offset))
        roots, counts = {}, {}
        items = []
        for row in rows:
            item = ticket_dict(row)
            item["criteria_v"] = item["criteria_version"]
            root = roots.get(item["project_id"])
            if root is None:
                owner = conn.execute("SELECT root FROM projects WHERE id=?", (item["project_id"],)).fetchone()
                root = roots[item["project_id"]] = owner["root"] if owner is not None else ""
            item["behind"] = behind_count(root, latest_head_sha(conn, item["id"]), counts)
            items.append(item)
        return {"columns": list(LIST_COLUMNS), "items": items, "limit": limit, "offset": offset}

    def _planned_view(self, conn, pid, limit, offset):
        """池子视图（PRD-014 R6）：按依赖层数升序，每张带 claimable 与 blocked_by。

        两列只在这个视图出现（其余视图的表头与以前逐字一致）。`claimable` 的定义写死为
        「状态可领 且 blocked_by 全部 done」，它**不代表「现在就能领」**：同项目已有在途单时
        `claim_ticket` 仍报 `DISPATCH_BUSY`，那是另一件事，不往这一列里混着答（AC-R6-3）。
        """
        rows = conn.execute("SELECT * FROM tickets WHERE state=? AND (? IS NULL OR project_id=?)"
                            " ORDER BY created_at,id", (PLANNED_STATE, pid, pid)).fetchall()
        depths = dependency_depths(conn, [row["id"] for row in rows])
        ordered = sorted(rows, key=lambda row: (depths.get(row["id"], 0), row["created_at"], row["id"]))
        roots, counts, items = {}, {}, []
        for row in ordered:
            item = ticket_dict(row)
            item["criteria_v"] = item["criteria_version"]
            root = roots.get(item["project_id"])
            if root is None:
                owner = conn.execute("SELECT root FROM projects WHERE id=?", (item["project_id"],)).fetchone()
                root = roots[item["project_id"]] = owner["root"] if owner is not None else ""
            item["behind"] = behind_count(root, latest_head_sha(conn, item["id"]), counts)
            pending = pending_blockers(conn, item["id"])
            item["blocked_by"] = [blocker["id"] for blocker in pending]
            item["claimable"] = item["state"] in CLAIMABLE_STATES and not pending
            # 0 条条件 = 还是念头：它进不了 claim（那里会报 CRITERIA_NOT_JUDGEABLE）。
            item["criteria_n"] = len(item["criteria"])
            items.append(item)
        return {"columns": list(LIST_COLUMNS) + list(PLANNED_COLUMNS),
                "items": items[offset:offset + limit], "limit": limit, "offset": offset}

    def _unchanged_prd(self, conn, ticket, attempt=None):
        """这次动作依据的那一版 PRD 有没有变。

        比较对象是**尝试上记下的那一份**（PRD-014 R2.3）：依据在 claim 那一刻定格，
        之后 PRD 再变就是 `STALE_PRD`，哪怕工单行上的值因为别的动作变了。
        没有尝试可依时才退回工单行上的那一份（旧调用方、尚未派发的现场）。
        """
        if attempt is None and ticket["current_attempt"]:
            attempt = conn.execute("SELECT * FROM attempts WHERE id=?", (ticket["current_attempt"],)).fetchone()
        basis = attempt if attempt is not None else ticket
        project = self._project(conn, ticket["project_id"])
        prd = fingerprint(project["root"], basis["prd_path"])
        require(prd["sha256"] == basis["prd_sha256"], "STALE_PRD",
                "PRD changed; explicitly revise the ticket before proceeding")
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
        # 取消先判，排在 revision 之前（AW-159）：读到旧 revision 时报的 STALE_REVISION 说的是
        # 「重读一次再来」，可在取消之后重读多少次都过不去。先报那个让重试失去意义的事实。
        # 闸门写在这里而不是抽成一个函数，是因为要读的人在第一眼就问「这张单能不能领」，
        # 不该先跳一次定义；三处的判据同一句、断语同一句（`cancelled_message`）。
        require(ticket["state"] != "cancelled", "TICKET_CANCELLED", cancelled_message(ticket, "claim_ticket"))
        self._expected_revision(ticket, data)
        require(ticket["state"] in CLAIMABLE_STATES, "INVALID_STATE",
                "Ticket is not ready to be claimed (planned, ready and rework can be claimed)")
        # 空条件不许开工（AW-109）：`planned` 允许 0 条，但那是「念头」，不是「可以验收的活」。
        # 与下面那条依赖校验同性：它们回答的都是「这张单现在能不能开工」，所以排在
        # DISPATCH_BUSY 之前（PRD-014 R3.1 的判序：先答能不能开工，再答项目忙不忙）。
        require(json.loads(ticket["criteria"]), "CRITERIA_NOT_JUDGEABLE",
                f"{ticket['id']} has no criteria yet: it is a thought parked in the pool, not a spec. "
                "Call revise_ticket with at least one judgeable criterion first (or cancel_ticket); "
                "nothing was written")
        # 依赖先判：它回答的是「这张单现在能不能开工」，比「项目忙不忙」更靠近事实，
        # 也更容易照着下一步做（PRD-014 R3.1）。未完成就点名，一行不写。
        pending = pending_blockers(conn, ticket["id"])
        if pending:
            listed = ", ".join(f"{row['id']} ({row['state']})" for row in pending)
            raise Problem("BLOCKED_BY_DEPENDENCY",
                          f"{ticket['id']} is blocked by {listed}; only done satisfies blocked_by "
                          "(a cancelled prerequisite is not done), so the ticket is left in its current state")
        # 串行是默认值，不是礼貌建议：同项目还有在途工单就拒绝，并点名是哪一张、停在哪一步。
        # 越过要显式传 allow_parallel=true，越过时留一条事件，事后看得出这是故意的。
        allow_parallel = data.get("allow_parallel", False)
        require(type(allow_parallel) is bool, "INVALID_INPUT", "allow_parallel must be a boolean")
        # 并行是声明，不是隐形开关：越过的同时必须写清为什么，可选写清占哪些路径/资源。
        # 校验在任何写入之前，所以缺理由时工单停在原状态，一个尝试都不产生。
        if not allow_parallel:
            require("parallel_reason" not in data and "parallel_scope" not in data, "INVALID_INPUT",
                    "parallel_reason and parallel_scope only make sense together with allow_parallel=true")
        reason, scope = None, None
        if allow_parallel:
            given = data.get("parallel_reason")
            require(isinstance(given, str) and bool(given.strip()) and len(given) <= 200,
                    "PARALLEL_REASON_REQUIRED",
                    "allow_parallel=true requires parallel_reason: 1 to 200 characters saying why these tickets "
                    "can run at the same time; the ticket is left in its original state")
            reason = given
            if "parallel_scope" in data:
                scope = data["parallel_scope"]
                require(isinstance(scope, list) and 1 <= len(scope) <= 20, "INVALID_INPUT",
                        "parallel_scope must contain 1 to 20 strings")
                for entry in scope:
                    require(isinstance(entry, str) and bool(entry.strip()) and len(entry) <= 500,
                            "INVALID_INPUT", "Each parallel_scope entry must be a nonempty string of at most 500 characters")
        # 派发者与执行者必须是两个会话：claim 时写下谁派的，bind 时拒绝同一个会话。
        coordinator = text(data, "coordinator_session_id", 500)
        # 车次归属（PRD-014 R4.2）：带着车次的单在这里做一次运行期校验。它不用唯一键表达，
        # 因为唯一键说不出「同一会话可以持多张、别的会话不能进同一车次」这条规则。
        # 校验在任何写入之前，被拒时一个尝试、一条工单改动都不留（只留一行拒收事件）。
        # 「本会话」在 claim 阶段能拿到的只有 coordinator_session_id：执行的会话要到 bind 才有，
        # 所以车次认领的就是 claim 的这条会话，owner 之后不随 bind / recover 改变。
        #
        # 判序（AW-112，从 PRD-014 推出，不是择一面取）：车次这一段的三种拒绝都排在
        # `DISPATCH_BUSY` 之前。两个理由：
        #   1. R4.2 把车次归属校验归成「与 R3 的 BLOCKED_BY_DEPENDENCY 同一类显式校验」，
        #      而 R3.1 定下的判序是「先回答这张单能不能开工，再回答项目忙不忙」（依赖那道
        #      校验就明写在 DISPATCH_BUSY 之前）。同类问题按同一条判序，不按代码位置。
        #   2. 两条的出口不对称：DISPATCH_BUSY 给了 allow_parallel 这条出路，车次是别人的
        #      这句话没有出路。先报能绕的那个，调用方照它换参数再来一次，下一次仍然过不去；
        #      报不可绕的那个，一句话就能把人送到对的会话手上。
        # 同一现场两个条件都满足时报的是 `BATCH_OWNED_BY_OTHER`（用例
        # `test_batch_error_before_busy` 固定这一处）。
        batch_id = ticket["batch_id"]
        batch_owner = None
        if batch_id is not None:
            batch = self._batch_of_ticket(conn, ticket)
            # 外键已经要求车次存在；这条只防「库里出现指向不存在车次的单」这种脏行。
            require(batch is not None, "INVALID_STATE",
                    f"Ticket {ticket['id']} names batch {batch_id}, which does not exist; fix the row before claiming")
            require(not batch["closed_at"], "INVALID_STATE",
                    f"Batch {batch_id} was closed at {batch['closed_at']}; a closed trip accepts no new ticket")
            batch_owner = batch["owner_session_id"]
            if batch_owner:
                require(batch_owner == coordinator, "BATCH_OWNED_BY_OTHER",
                        f"Batch {batch_id} is owned by session {batch_owner}; another session cannot claim its "
                        "tickets. Tickets of one trip are claimed by the session that owns it, so hand this "
                        "ticket back to that session or build a new trip.")
        busy = self._busy_tickets(conn, ticket)
        if busy and not allow_parallel:
            listed = ", ".join(f"{row['id']} ({row['state']})" for row in busy)
            raise Problem("DISPATCH_BUSY",
                          f"Project {ticket['project_id']} already has a ticket in flight: {listed}; "
                          "finish it (merge or cancel) before claiming another, or pass allow_parallel=true")
        parallel_with = [{"ticket_id": row["id"], "state": row["state"]} for row in busy]
        # 指纹在这一刻定格（PRD-014 R2.2）：取指纹的是 claim 自己，不交给定时任务或执行会话。
        # 排队期间 PRD 升版因此不再让单过期；claim 之后再变才是 STALE_PRD。
        project = self._project(conn, ticket["project_id"])
        prd = fingerprint(project["root"], ticket["prd_path"])
        # 协调者自己的工作目录是可选的自报值：不传就空，不阻止派发（PRD-009 R5）。
        workdir = text(data, "coordinator_workdir", 1000) if "coordinator_workdir" in data else None
        if batch_id is not None and not batch_owner:
            # 车次第一次被领：写入持有会话。这一句之前的每一条 require 都已经过了，
            # 所以被拒的调用不会顺手把车次认走。
            conn.execute("UPDATE batches SET owner_session_id=? WHERE id=?", (coordinator, batch_id))
        aid = str(uuid4())
        conn.execute("INSERT INTO attempts(id,ticket_id,revision,criteria_version,prd_path,prd_sha256,state,session_id,"
                     "coordinator_session_id,coordinator_workdir,created_at,batch_key)"
                     " VALUES (?,?,?,?,?,?,'dispatching',NULL,?,?,?,?)",
                     (aid, ticket["id"], ticket["revision"], ticket["criteria_version"], prd["path"],
                      prd["sha256"], coordinator, workdir, now(), batch_id))
        # 工单行也回写这一版指纹：「这一次派发依据的是哪一版」。
        # 同一句里认领责任人：claim 的这条会话就是这张单现在的 owner（C5 / AW-114）。
        # 它与刚写的 `coordinator_session_id` 此刻相等，但那是两条独立的事实——owner
        # 之后可以被 `take_over_ticket` 换掉，origin 永远是谁 claim 的那条会话。
        conn.execute("UPDATE tickets SET state='dispatching',current_attempt=?,prd_path=?,prd_sha256=?,"
                     "owner_session_id=? WHERE id=?",
                     (aid, prd["path"], prd["sha256"], coordinator, ticket["id"]))
        if allow_parallel:
            # 事由与范围与 parallel_with 并列写进同一条事件：后来的人要判「这次并行合不合理」
            # 只能靠这一行，事由散落在消息或会话里就查不回来了（PRD-013 R2）。
            detail = {"allow_parallel_override": True, "parallel_with": parallel_with,
                      "parallel_reason": reason}
            if scope is not None:
                detail["parallel_scope"] = scope
            conn.execute("INSERT INTO events(ticket_id,operation,detail,created_at) VALUES (?,?,?,?)",
                         (ticket["id"], "claim_ticket", packed(detail), now()))
        return {"ticket_id": ticket["id"], "project_id": ticket["project_id"], "attempt_id": aid,
                "revision": ticket["revision"], "state": "dispatching",
                "prd_path": prd["path"], "prd_sha256": prd["sha256"],
                "batch_id": batch_id,
                "batch_owner_session_id": (batch_owner or coordinator) if batch_id is not None else None,
                "coordinator_session_id": coordinator, "coordinator_workdir": workdir,
                "allow_parallel": allow_parallel, "parallel_with": parallel_with,
                "parallel_reason": reason, "parallel_scope": scope}

    def _bind_session(self, conn, data):
        ticket, attempt = self._active(conn, data)
        require(ticket["state"] == "dispatching", "INVALID_STATE", "Attempt is not awaiting a session binding")
        # 协调会话不能执行自己派发的工单；拒绝必须发生在任何写入之前，工单停在 dispatching。
        session = text(data, "session_id", 500)
        require(session != attempt["coordinator_session_id"], "SELF_DISPATCH",
                "The coordinator that claimed this ticket cannot execute it; open a separate session")
        project = self._unchanged_prd(conn, ticket, attempt)
        require(project_root(data.get("project_root")) == project["root"], "PROJECT_MISMATCH", "Session project directory does not match the registered project")
        conn.execute("UPDATE attempts SET session_id=?,state='running' WHERE id=?", (session, attempt["id"]))
        conn.execute("UPDATE tickets SET state='running' WHERE id=?", (ticket["id"],))
        return {"attempt_id": attempt["id"], "session_id": session,
                "coordinator_session_id": attempt["coordinator_session_id"],
                "state": "running", "host_identity_verified": False}

    def _submit_result(self, conn, data):
        # 取消闸门排在 `_active` 之前（AW-159）：取消会把 `current_attempt` 置空，所以
        # `_active` 在这里报的是 STALE_ATTEMPT——「这不是当前那次尝试」，听起来像换一个
        # attempt_id 就能提交。工单已经取消时先说取消。
        ticket = self._ticket(conn, data)
        require(ticket["state"] != "cancelled", "TICKET_CANCELLED", cancelled_message(ticket, "submit_result"))
        ticket, attempt = self._active(conn, data, check_session=True)
        require(ticket["state"] == "running", "INVALID_STATE", "Ticket is not accepting execution results")
        project = self._unchanged_prd(conn, ticket, attempt)
        # 两棵树都必填：`head_sha` 说「交的是哪一棵」，`base_sha` 说「它从哪分出来的」。
        # 但业务项目根不是 git 仓库时没有可填的树（AW-151）：执行会话显式声明
        # `repo_kind="non_git"`，两棵树留 NULL——不拿空树值（da39a3ee…）之类的占位 sha
        # 顶替，那会把「没有树」写成「有一棵树」。非 git 项目没有提交可合，验收后由
        # 协调会话 `archive_ticket` 归位，`tools/unmerged_scan.py` 也按项目根是不是
        # git 仓库把它归进「仓库无改动」，不进「真待合并」。
        repo_kind = data.get("repo_kind", "git")
        require(repo_kind in ("git", "non_git"), "INVALID_INPUT",
                'repo_kind must be "git" or "non_git"')
        if repo_kind == "non_git":
            require("head_sha" not in data and "base_sha" not in data, "INVALID_INPUT",
                    "repo_kind=non_git submissions must omit head_sha and base_sha")
            head_sha = base_sha = None
        else:
            # 先校它们再碰文件：缺字段时不至于因为另一个原因报出 FILE_NOT_FOUND。
            head_sha = sha_text(data, "head_sha")
            base_sha = sha_text(data, "base_sha")
        paths = string_list(data, "artifacts", minimum=1, maximum=20)
        artifacts = [fingerprint(project["root"], path) for path in paths]
        # 第一道键集合校验（AW-159）：提交侧自己生成了什么，就在这里按同一把尺查什么。
        # 它正常情况下永远不会红（`fingerprint()` 只写三个键），价值在「将来有人给它加
        # 一个键」那一刻当场报出字段名，而不是等到验收时才表现成一句无解的 ARTIFACT_CHANGED。
        for index, artifact in enumerate(artifacts):
            check_artifact(artifact, f"submit_result 生成的 artifacts[{index}]")
        require(len({str(Path(a["path"])).casefold() for a in artifacts}) == len(artifacts),
                "INVALID_INPUT", "Duplicate artifact paths")
        summary = text(data, "summary")
        evidence, risks = string_list(data, "evidence", minimum=1), string_list(data, "risks")
        # 这次尝试是按哪一版判据派发的：低于工单当前的版本，就说明它在开工后被改过，
        # 验收得按新条件重看一遍。这一句不回写任何行（PRD-013 R6）。
        criteria_changed = attempt["criteria_version"] < ticket["criteria_version"]
        # 冲突预演在提交写入之前算：它是一份现场事实，不是闸门——算不出来就存 NULL，
        # 会撞也照收（PRD-015 R2）。没有树（non_git）就没有可预演的提交。
        preview = conflict_preview(project["root"], head_sha) if head_sha else None
        sid = str(uuid4())
        conn.execute("INSERT INTO submissions(id,attempt_id,summary,artifacts,evidence,risks,created_at,head_sha,"
                     "base_sha,conflict_preview) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (sid, attempt["id"], summary, packed(artifacts), packed(evidence), packed(risks), now(),
                      head_sha, base_sha, None if preview is None else packed(preview)))
        conn.execute("UPDATE attempts SET state='submitted' WHERE id=?", (attempt["id"],))
        conn.execute("UPDATE tickets SET state='awaiting_review' WHERE id=?", (ticket["id"],))
        return {"submission_id": sid, "attempt_id": attempt["id"], "state": "awaiting_review",
                "artifacts": artifacts, "head_sha": head_sha, "base_sha": base_sha,
                "repo_kind": repo_kind,
                "conflict_preview": preview,
                "criteria_changed": criteria_changed,
                "criteria_version": {"attempt": attempt["criteria_version"],
                                     "ticket": ticket["criteria_version"]}}

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
        require(prd_sha256 == attempt["prd_sha256"], "STALE_PRD",
                "PRD changed while the question was waiting; revise the ticket or ask again")
        # 兜底：一条 open 问题配着非 blocked 的工单。正常路径上两者总是成对变化，
        # 所以这条不会被正常路径碰到；它防的是库里出现过一次不一致行之后，答案把别的状态挟持回 running。
        require(ticket["state"] == "blocked", "INVALID_STATE", "Ticket is not waiting for an answer")
        if answered_by == "coordinator":
            require(answerer != attempt["session_id"], "SELF_ANSWER",
                    "The executing session cannot answer its own question")
            # 同一把尺：代答的授权读**当前的负责人**（`tickets.owner_session_id`），
            # 不读「谁派的」（`attempts.coordinator_session_id` 是 origin，历史事实）。
            # 以前这里读 origin，而 `record_notice` 读 owner，于是同一张单上出现两把尺：
            # 派单它的调度会话被宿主归档之后，回执有人能补（接替之后），问题却谁都答不了
            # （AW-118：一张单在 `blocked` 里挂了 7 个多小时）。
            require(answerer == ticket["owner_session_id"], "NOT_COORDINATOR",
                    "Only the ticket's current owner may record an answer for a human")
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
        # 授权读**当前的负责人**（`tickets.owner_session_id`），不读「谁派的」那一条
        # （`attempts.coordinator_session_id` 是 origin，不可变的历史事实）。以前两者是
        # 同一个字符串，于是派发它的会话一被宿主归档，就再没有人能记回执——AW-106 就是
        # 这样被锁死的。owner 可以被 `take_over_ticket` 换掉，origin 永远不改。
        # 仍不是防伪：session_id 是自报字符串；这里对齐的是「谁说这声『发了』」。
        require(text(data, "session_id", 500) == ticket["owner_session_id"], "NOT_COORDINATOR",
                "Only the ticket's current owner may record a notice; if the owner session is gone, "
                "take the ticket over with take_over_ticket first")
        # 「发给谁」不能靠事后回想：投递目标必须是**现在**负责收尾的那条会话。写成执行
        # 会话自己是最容易发生的错，那条回执根本到不了人手里；把已归档的 origin 当成
        # 目标则是这条死锁的另一种写法。
        target = text(data, "target_session_id", 500)
        require(target == ticket["owner_session_id"], "NOT_TARGET",
                "target_session_id must be the ticket's current owner session")
        # 不属于白名单：宿主认得的值比本工具多，枚举外取值原样入库（见 wake_kind()）。
        # 只有 created 被特判，因为它是失败信号而不是另一种成功方式。
        wake = wake_kind(data)
        # 消息 id 可选：协调会话从发送回执里取到就传，取不到就是不传（NULL），不拿时间或其他东西凑。
        message = message_id(data) if "message_id" in data else None
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
        # 列名写全，不用位置插入：schema 再加一列时，位置插入会静默错位。
        conn.execute("INSERT INTO notices(id,ticket_id,attempt_id,origin_session_id,status,detail,"
                     "created_at,target_session_id,wake_kind,message_id)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (nid, ticket["id"], aid, origin, status, detail, now(), target, wake, message))
        # failed 可以多条（真会失败多次），它只留记录、不动工单状态：NOTIFY_FAILED 不推动任何状态机。
        result = {"notice_id": nid, "ticket_id": ticket["id"], "attempt_id": aid,
                  "origin_session_id": origin, "status": status, "detail": detail,
                  "target_session_id": target, "wake_kind": wake, "message_id": message,
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

    def _report_notice_failure(self, conn, data):
        """执行会话记自己那次没送到的回执，记在它自己那张单上（AW-114 C2）。

        为什么不能直接放开 `record_notice`：那个操作宣告的是「回执发到了」，只有协调
        会话有资格说。这里的宣告是反的——「我发不出去，宿主回了什么」——而且只能是反的：
        操作里根本没收 `status`，所以调用方连 `sent` 这个词都递不进来，`one_sent_notice`
        那条部分唯一索引在结构上碰不到。把两件事压进一个操作，等于给「执行会话宣告送达」
        留了一个只靠代码自觉拦着的位置。

        现场：协调会话是宿主建的，它自己那一轮结束时可能被归档（AW-114 实测
        `sessions.status='archived'`，`updated_at` 与 `last_turn_ended_at` 相差 22–26 ms）。
        归档之后执行会话的 `send_to_session` 只能拿到 `ARCHIVED`，那条回执以前没有任何
        落点，而 `merge_ticket` 又按 `NO_NOTICE` 拒合——一张已验收的单就这样卡死。

        谁记的：`notices` 不新增列（本单不升 schema），记录人落在成功事件里——
        `execute()` 会给每次成功写一行 `events`，`detail.result.reported_by_session_id`
        就是这张尝试绑定的执行会话。回执行自己的 `origin_session_id` 同样写它：这份回执
        的来源就是它，不是那条已经不在的协调会话。
        """
        # check_session：只有这张尝试绑定的执行会话能记。协调会话与路人会话都报
        # SESSION_MISMATCH，与 submit_result / ask_ticket 同一个判据，不另造一个码。
        ticket, attempt = self._active(conn, data, check_session=True)
        detail = text(data, "detail")
        # 投递目标必须仍是 claim 时写下的那条协调会话：它是「这次回执本来要发给谁」的
        # 唯一答案。接替之后再记时，这一列指向接替会话，而真正的失败目标写进 detail。
        target = text(data, "target_session_id", 500)
        require(target == attempt["coordinator_session_id"], "NOT_TARGET",
                "target_session_id must be the coordinator session recorded on this attempt")
        # 这一次没有宿主投递回执可读（send_to_session 返回的是错误码，不是 wake_kind），
        # 所以不给值是常态，落 `unknown`；给了就照样只校形状、原值入库。
        wake = wake_kind(data) if "wake_kind" in data else "unknown"
        nid = str(uuid4())
        conn.execute("INSERT INTO notices(id,ticket_id,attempt_id,origin_session_id,status,detail,"
                     "created_at,target_session_id,wake_kind) VALUES (?,?,?,?,?,?,?,?,?)",
                     (nid, ticket["id"], attempt["id"], attempt["session_id"], "failed", detail,
                      now(), target, wake))
        return {"notice_id": nid, "ticket_id": ticket["id"], "attempt_id": attempt["id"],
                "reported_by_session_id": attempt["session_id"],
                "origin_session_id": attempt["session_id"], "status": "failed", "detail": detail,
                "target_session_id": target, "wake_kind": wake, "alert": NOTIFY_FAILED,
                "ticket_state": ticket["state"], "revision": ticket["revision"]}

    def _take_over_ticket(self, conn, data):
        """接替责任人：把 `tickets.owner_session_id` 换成一条新会话（AW-114 C3/C5/C6）。

        为什么不是改 `attempts.coordinator_session_id`：那个字段说的是「谁派的」，是一次
        历史事实，改掉就再也查不出当初是谁派的单；而「现在谁负责收尾」是会变的东西
        （原会话被宿主归档、人换了、巡检接手）。一个字段担两种语义的代价，AW-106 已经
        付过一次：派发它的会话被归档之后，`record_notice` 的 NOT_COORDINATOR 与
        `merge_ticket` 的 NO_NOTICE 合起来把一张已验收的单锁死。

        所以这里只动 owner：

        - **origin 不动。** `attempts.coordinator_session_id` 保持 claim 时写下的值，
          回执 `target_session_id` 与 `report_notice_failure` 的目标仍以它为准。
        - **不改代次。** `revision`、`current_attempt`、提交与验收原样；接替不是新的一代。
        - **留痕。** 成功事件由 `execute()` 照常写一行 `operation=take_over_ticket`，
          `detail.result` 里新旧 owner 与理由都在。
        - **一次自报声明，不是身份认证。** `new_owner_session_id` 仍是字符串，工具验证
          不了宿主里那条会话是否活着。能守的两条写在下面：执行会话不能给自己接替
          （那等于自派自做），同一条会话不能重复接替。
        """
        ticket = self._ticket(conn, data)
        require(ticket["state"] not in ("cancelled", ARCHIVED_STATE), "INVALID_STATE",
                "Cancelled or archived tickets have no wrap-up work left to take over")
        owner = text(data, "new_owner_session_id", 500)
        reason = text(data, "reason")
        require(owner != ticket["owner_session_id"], "ALREADY_OWNER",
                "This session is already the ticket's owner; there is nothing to take over")
        if ticket["current_attempt"]:
            attempt = conn.execute("SELECT session_id FROM attempts WHERE id=?",
                                   (ticket["current_attempt"],)).fetchone()
            require(attempt is None or attempt["session_id"] != owner, "TAKEOVER_BY_EXECUTOR",
                    "The executing session cannot take over ownership of its own ticket; a separate "
                    "session has to declare it")
        previous = ticket["owner_session_id"]
        conn.execute("UPDATE tickets SET owner_session_id=? WHERE id=?", (owner, ticket["id"]))
        origin = None
        if ticket["current_attempt"]:
            row = conn.execute("SELECT coordinator_session_id FROM attempts WHERE id=?",
                               (ticket["current_attempt"],)).fetchone()
            origin = row["coordinator_session_id"] if row is not None else None
        return {"ticket_id": ticket["id"], "previous_owner_session_id": previous,
                "owner_session_id": owner, "origin_session_id": origin, "reason": reason,
                "state": ticket["state"], "revision": ticket["revision"]}

    def _review_dispatched(self, conn, data):
        """记下「验收已经派出去了」（AW-133）：一行事件，别的一行不动。

        要解决的不是「验收怎么判」，而是同一个现场连着几轮都被判成 `dispatch_review`：
        `next_action.py` 只看「在等验收 + 没有 `reviews` 行」，而派出去的验收会话要跑
        几分钟才落结论，在这段窗口里库与几轮之前一模一样。它只能读出「该派验收」，
        于是同一张单派了第二个、第三个验收会话。

        所以这里只落一条可判的事实 `review_dispatched`，三个边界都不碰：

        - **不改工单状态。** 还是 `awaiting_review`，`revision` 不动——派验收不是新的一代。
        - **不碰 `reviews` 表。** 验收结论只有验收会话能写（`review_result`）；这条事件不是
          结论，它只说「有人在看」，所以它有窗口期（`REVIEW_IN_FLIGHT_SECONDS`），过了就失效。
        - **不认证。** `reviewer_session_id` 可给可不给，给了也只是一条自报的指向，不是身份。

        判序按「这张单现在能不能被派验收」来排队：状态不对 → 没有提交 → 提交不是最新那条
        （旧提交已被返工换掉）→ 已有验收结论。四种都一行不写，只留一条拒绝事件。
        """
        ticket = self._ticket(conn, data)
        require(ticket["state"] == "awaiting_review", "INVALID_STATE",
                "Only a ticket waiting for review can be marked as dispatched; "
                f"this one is {ticket['state']}")
        submission_id = text(data, "submission_id", 200)
        latest = conn.execute(
            "SELECT s.id FROM submissions s JOIN attempts a ON a.id=s.attempt_id"
            " WHERE a.ticket_id=? ORDER BY s.created_at DESC, s.rowid DESC LIMIT 1",
            (ticket["id"],)).fetchone()
        require(latest is not None, "INVALID_STATE",
                "This ticket has no submission, so there is nothing to dispatch a review for")
        require(latest["id"] == submission_id, "STALE_SUBMISSION",
                f"The latest submission of this ticket is {latest['id']}; mark that one, not {submission_id}")
        require(conn.execute("SELECT 1 FROM reviews WHERE submission_id=?", (submission_id,)).fetchone() is None,
                "ALREADY_REVIEWED",
                "This submission already has a review result; a second review would judge the same tree twice")
        reviewer = text(data, "reviewer_session_id", 500) if data.get("reviewer_session_id") else None
        note = text(data, "note") if data.get("note") else None
        # 事件正文由 execute() 写成 {request_id, result}，`next_action` 读 result 里的这两项。
        return {"ticket_id": ticket["id"], "state": ticket["state"],
                "revision": ticket["revision"], "submission_id": submission_id,
                "reviewer_session_id": reviewer, "note": note,
                "in_flight_until": (datetime.now(timezone.utc)
                                    + timedelta(seconds=REVIEW_IN_FLIGHT_SECONDS)).isoformat()}

    def _record_pool_report(self, conn, data):
        """记一笔「这张空判据单已经被点名报过」（AW-140）。

        `tools/next_action.py` 的空判据档（`fix_pool`）只报不改，所以「已经报过几次」
        不能由它自己写——它拿到的库是 `mode=ro`。这条事件就是那一侧的落库位置：
        一条行 = 这张单被点名一次，读侧按条数决定「这一轮照不照原样报」与「要不要升级」。

        三个边界与 `review_dispatched` 同形：

        - **不改工单状态。** 还是 `planned` / `ready` / `rework`，`revision` 与
          `criteria_version` 都不动——报过不是新的一代，更不是补了判据。
        - **不写判据、不取消单。** 那两件事只有人能做（`revise_ticket` / `cancel_ticket`）。
        - **不认证。** `session_id` 可给可不给，给了也只是一条自报的指向。

        判据只有三条：工单存在；状态可领（空判据只在这三个状态里表达得出来）；
        条件确实为空。条件不为空报 `INVALID_STATE`——把有判据的单记成「空判据报过」，
        等于让读侧替一张真活记账。
        """
        ticket = self._ticket(conn, data)
        require(ticket["state"] in CLAIMABLE_STATES, "INVALID_STATE",
                "Only a claimable ticket can be recorded as reported; "
                f"this one is {ticket['state']}")
        try:
            items = json.loads(ticket["criteria"] or "[]")
        except (TypeError, ValueError):
            raise Problem("INVALID_STATE",
                          "This ticket's criteria cannot be read; fix that row instead of "
                          "recording a blank-criteria report")
        require(items == [], "INVALID_STATE",
                "This ticket has criteria; only a blank-criteria ticket can be recorded as reported")
        session_id = text(data, "session_id", 500) if data.get("session_id") else None
        note = text(data, "note") if data.get("note") else None
        # 轮次由读侧按行数算，这里只回显「记完之后是第几轮」，免得调用方自己数。
        rounds = 1 + conn.execute(
            "SELECT COUNT(*) FROM events WHERE ticket_id=? AND operation=?",
            (ticket["id"], POOL_REPORT)).fetchone()[0]
        return {"ticket_id": ticket["id"], "state": ticket["state"],
                "revision": ticket["revision"], "session_id": session_id, "note": note,
                "reported_rounds": rounds, "reported_at": now()}

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
        # 与 claim 同一道闸、同一个判序（AW-159）：取消后的单不能被恢复，先报取消再报版本。
        require(ticket["state"] != "cancelled", "TICKET_CANCELLED", cancelled_message(ticket, "recover_ticket"))
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

    def _in_flight(self, conn, ticket):
        """当前尝试还在飞就返回它，否则 None。用来看「改判据要不要叫醒谁」。"""
        if not ticket["current_attempt"]:
            return None
        attempt = conn.execute("SELECT * FROM attempts WHERE id=?", (ticket["current_attempt"],)).fetchone()
        if attempt is None or attempt["state"] not in IN_FLIGHT_ATTEMPT_STATES:
            return None
        return attempt

    def _revise_ticket(self, conn, data):
        """改需求摘要与验收条件；默认**不作废**在飞的尝试（PRD-013 R6）。

        一个数管一件事：`criteria_version` 只随判据变，`revision` 只随换会话或重开变。
        以前这里无条件调 `_invalidate`，于是「判据变了」与「这一代作废了」被压成一个动作，
        执行会话拿着过期标准继续干，而验收只看得到 revision 跳了。要作废就显式写
        `restart: true`——那是三个作废入口之一，不再是谁都没注意到的副作用。
        """
        ticket = self._ticket(conn, data)
        self._expected_revision(ticket, data)
        require(ticket["state"] != "cancelled", "INVALID_STATE", "Cancelled tickets cannot be silently reopened")
        reopen = data.get("reopen", False)
        restart = data.get("restart", False)
        require(type(reopen) is bool, "INVALID_INPUT", "reopen must be a boolean")
        require(type(restart) is bool, "INVALID_INPUT", "restart must be a boolean")
        require(ticket["state"] != "done" or (reopen or restart), "INVALID_STATE",
                "Revising a delivered ticket reopens it; pass reopen: true to confirm")
        # 重开已完成的单也是换代次（revision 只随换会话或重开变）；restart 同理。
        restarting = restart or (reopen and ticket["state"] == "done")
        # 条件要按**落点状态**判，不是按改之前那个状态判（AW-109 返工 C1）：`restart` 与
        # `reopen` 都把单送回 `ready`，而 `planned` 是唯一允许空条件的状态。按当前状态判，
        # 一张 planned+[] 单走 `revise_ticket(restart=true)` 会落成 `state=ready` 且
        # `criteria=[]`——它谁都领不走（claim 报 CRITERIA_NOT_JUDGEABLE），
        # docs/protocol.md 那句「`ready` / `rework` / 在飞的单必须至少 1 项」也就成了假话。
        # 池子里的单仍然可以反复改描述而条件照旧空着（落点仍是 planned）。
        reason, checks = text(data, "reason"), criteria(
            data, "ready" if restarting else ticket["state"])
        deliverable = ticket["deliverable"]
        if "deliverable" in data:
            deliverable = text(data, "deliverable", 500)
        previous_state = ticket["state"]
        project = self._project(conn, ticket["project_id"])
        prd = fingerprint(project["root"], text(data, "prd_path"))
        links = link_payload(data)
        # planned 单排队期间不取指纹（PRD-014 R2.4）：改了路径也留 NULL，claim 时才定格。
        frozen_sha256 = None if ticket["state"] == PLANNED_STATE else prd["sha256"]
        # 判据真的变了才 +1：只改交付物描述、或原封不动重交一份，都不是新一版判据。
        changed = checks != json.loads(ticket["criteria"])
        version_from = ticket["criteria_version"]
        version_to = version_from + 1 if changed else version_from
        must_wake = None
        in_flight = None if restarting else self._in_flight(conn, ticket)
        if restarting:
            self._invalidate(conn, ticket, "ready")
        else:
            if in_flight is not None and changed:
                # 不作废不等于什么都不说：回执点明要叫谁、为什么叫。消息发不发归协调会话。
                must_wake = {"session_id": in_flight["session_id"], "attempt_id": in_flight["id"],
                             "criteria_version": {"from": version_from, "to": version_to}}
        conn.execute("UPDATE tickets SET prd_path=?,prd_sha256=?,criteria=?,deliverable=?,criteria_version=?"
                     " WHERE id=?",
                     (prd["path"], frozen_sha256, packed(checks), deliverable, version_to, ticket["id"]))
        if in_flight is not None:
            # 在飞尝试上的那一份也换掉：`_unchanged_prd()` 比的是 attempt 上的值，只改工单行
            # 会让「改文件 → revise_ticket → 接着提交」这条文档承诺的路必然报 STALE_PRD
            # （AW-109 实测）。不传 restart 时旧尝试照旧在飞，依据跟着换成新文件；要作废就
            # 走 restart，那条分支由 _invalidate 接管，这里不动。
            conn.execute("UPDATE attempts SET prd_path=?,prd_sha256=? WHERE id=?",
                         (prd["path"], prd["sha256"], in_flight["id"]))
        if links:
            # 与 create 同一道写边口：先清后写、写完查环，成环整个事务回滚（PRD-014 R3.3）。
            self._check_link_targets(conn, ticket["id"], links)
            self._write_links(conn, ticket["id"], links)
        if must_wake is not None:
            conn.execute("INSERT INTO events(ticket_id,operation,detail,created_at) VALUES (?,?,?,?)",
                         (ticket["id"], "criteria_revised_in_flight",
                          packed({**must_wake, "reason": reason}), now()))
        return {"ticket_id": ticket["id"],
                "state": "ready" if restarting else ticket["state"],
                "previous_state": previous_state,
                "revision": ticket["revision"] + (1 if restarting else 0),
                "criteria_version": {"from": version_from, "to": version_to},
                "must_wake": must_wake,
                "reason": reason, "prd_path": prd["path"],
                "prd_sha256": frozen_sha256, "criteria": checks, "deliverable": deliverable}

    def _cancel_ticket(self, conn, data):
        ticket = self._ticket(conn, data)
        self._expected_revision(ticket, data)
        reason = text(data, "reason")
        require(ticket["state"] not in ("done", "cancelled", ARCHIVED_STATE), "INVALID_STATE",
                "Ticket is already terminal")
        self._invalidate(conn, ticket, "cancelled")
        return {"ticket_id": ticket["id"], "state": "cancelled", "revision": ticket["revision"] + 1, "reason": reason}

    def _archive_ticket(self, conn, data):
        """`done` → `archived`：承认「验收过、但账对不上」的存量单，不动它的历史（AW-128）。

        判序是**先状态、后 revision**，与别的动作相反，而且是有意的：同一张单的第二次
        归档要报 `INVALID_STATE`（它已经不是 `done` 了），不是 `STALE_REVISION`（那说的是
        调用方读到的版本旧）。两者混起来以后，人看到 `STALE_REVISION` 会以为「重读一次再
        试就能成」，于是真的会去重试，而这条路永远走不通。

        `revision` **不变**：归档不是新的一代，它没有作废任何尝试、也没有换人。所以也
        不走 `_invalidate()`——那个函数会把 `current_attempt` 置空、把在飞尝试标成
        `superseded`，而归档单的当前尝试早就 `accepted` 了，抹掉这条指针等于把
        「哪一次尝试被验收的」从工单行上删掉。历史行一条不删，正是这个状态存在的理由。
        """
        ticket = self._ticket(conn, data)
        reason = text(data, "reason")
        require(ticket["state"] == "done", "INVALID_STATE",
                f"{ticket['id']} 当前 state={ticket['state']}；只有 done 的单能归档，且归档之后"
                f"不能再归档一次（revision {ticket['revision']} 未变，历史行一条没删）")
        self._expected_revision(ticket, data)
        conn.execute("UPDATE tickets SET state=? WHERE id=?", (ARCHIVED_STATE, ticket["id"]))
        return {"ticket_id": ticket["id"], "project_id": ticket["project_id"],
                "state": ARCHIVED_STATE, "previous_state": "done",
                "revision": ticket["revision"], "current_attempt": ticket["current_attempt"],
                "reason": reason}

    def _review_result(self, conn, data):
        ticket, attempt = self._active(conn, data)
        require(ticket["state"] == "awaiting_review", "INVALID_STATE", "Ticket is not awaiting review")
        project = self._unchanged_prd(conn, ticket, attempt)
        submission = conn.execute("SELECT * FROM submissions WHERE id=? AND attempt_id=?",
                                  (text(data, "submission_id", 200), attempt["id"])).fetchone()
        require(submission is not None, "SUBMISSION_MISMATCH", "Submission is not from this execution attempt")
        reviewer = text(data, "reviewer_session_id", 500)
        executed = conn.execute("SELECT 1 FROM attempts WHERE ticket_id=? AND session_id=?", (ticket["id"], reviewer)).fetchone()
        require(executed is None, "SELF_REVIEW", "A session that executed this ticket cannot review it")
        verdict, reason = text(data, "verdict", 20), text(data, "reason")
        require(verdict in ("accept", "reject"), "INVALID_INPUT", "Verdict must be accept or reject")
        checks = review_checks(json.loads(ticket["criteria"]), data.get("checks"))
        tally = checks_summary(checks)
        # 三档之后「通过」只表示没有失败项：未验不阻断 accept，但会被数出来（AW-132）。
        # 反过来 reject 仍必须有失败项——把未验当理由打回，等于对没验的东西下「不合格」。
        require(verdict != "accept" or not tally["fail"], "INVALID_INPUT",
                "Accept cannot carry a failed check; a check nobody could verify is unverified, not failed")
        require(verdict != "reject" or tally["fail"], "INVALID_INPUT",
                "Reject requires at least one failed (fail) check; unverified is not a failure")
        for index, saved in enumerate(json.loads(submission["artifacts"])):
            # 第二道键集合校验（AW-159）：先确认这一行的形状，再去比指纹。顺序反过来的话，
            # 多一个键只会走到 `ARTIFACT_CHANGED`——那句话说的是「成果变了」，而事实是
            # 「这一行的形状不是提交时那个形状」，两件事要的处置完全不同。
            check_artifact(saved, f"submission {submission['id']} 的 artifacts[{index}]")
            try:
                current = fingerprint(project["root"], saved["path"])
            except Problem as exc:
                require(False, "ARTIFACT_CHANGED",
                        f"提交的成果文件 {saved['path']} 现在核对不了（{exc.code}：{exc}）；"
                        "恢复后重新提交再验收")
            require(current == saved, "ARTIFACT_CHANGED",
                    f"提交的成果文件 {saved['path']} 与提交时不一致：{artifact_drift(saved, current)}；"
                    "恢复后重新提交，按新指纹再验收")
        review_id, state = str(uuid4()), "done" if verdict == "accept" else "rework"
        conn.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?,?)",
                     (review_id, submission["id"], reviewer, verdict, packed(checks), reason, now()))
        conn.execute("UPDATE tickets SET state=? WHERE id=?", (state, ticket["id"]))
        conn.execute("UPDATE attempts SET state=? WHERE id=?", ("accepted" if verdict == "accept" else "rejected", attempt["id"]))
        return {"review_id": review_id, "submission_id": submission["id"], "state": state,
                "checks": checks, "checks_summary": tally, "summary": review_summary_line(verdict, tally),
                "reason": reason}

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
        # 「这条合并记录是谁写的、当时闸门跑了哪几步」必须一眼分得开（AW-116 / AW-127）。
        # 普通合并记 `auto_merge`（工具自己跑的预演 + 临时工作树测试 + 合并后测试）；
        # 手工合并记 `manual`（人跑的 `git merge --no-ff`，闸门哪几步没跑写在 note）；
        # 事后补记档写 `backfill`。后两档都在这里**当场跑一次** `git merge-base
        # --is-ancestor` —— `verified_by` 记的是真跑过的命令，不是调用方抄来的一句描述。
        source, note, verified_by, verified_at = self._merge_provenance(conn, ticket, data, reviewed_sha)
        conn.execute("INSERT INTO merges(id,ticket_id,commit_sha,target_branch,tests,created_at,reviewed_sha,"
                     "source,verified_by,verified_at,note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (merge_id, ticket["id"], commit, target_branch, tests, now(), reviewed_sha,
                      source, verified_by, verified_at, note))
        return {"merge_id": merge_id, "ticket_id": ticket["id"], "project_id": ticket["project_id"],
                "commit": commit, "target_branch": target_branch, "tests": tests,
                "reviewed_sha": reviewed_sha, "source": source, "verified_by": verified_by,
                "verified_at": verified_at, "note": note}

    def _merge_provenance(self, conn, ticket, data, reviewed_sha):
        """合并记录的出处：谁写的、当时闸门跑了哪几步、拿什么核对的（AW-116 / AW-127）。

        四个取值而不是两个，各自回答「这条落库时闸门跑了哪几步」：

        - `auto_merge`：工具的正常合并路径——冲突预演、临时工作树测试、真实 merge、
          合并后测试都跑过，所以它不需要 note，也不收调用方写的辩解。
        - `manual`：人跑的 `git merge --no-ff`（例如 `auto_merge` 在该项目上跑不动）。
          闸门哪几步没跑只有写记录的人知道，所以**必须**留 note；能当场核的只有祖先
          关系，由工具自己跑并写进 `verified_by`。
        - `backfill`：记录是事后补的，note 写清补记时间与原因，同样当场核祖先关系。
        - NULL（存量行）：那时没有这一项，补一个值就是编。

        后两档都不接受调用方自报 `verified_by`：那会让「我核对过了」变成一句可以随便
        写的话。两档都要有可核对的树，`unverified` 在这里不算数。
        """
        source = text(data, "source", 20) if "source" in data else "auto_merge"
        require(source in ("auto_merge", "backfill", "manual"), "INVALID_INPUT",
                "source must be auto_merge, backfill or manual")
        note = text(data, "note", 1000) if "note" in data else None
        require(note is None or source != "auto_merge", "INVALID_INPUT",
                "note belongs to records written by hand (source=manual or backfill); "
                "source=auto_merge writes its own provenance")
        if source == "auto_merge":
            return "auto_merge", None, None, now()
        require(note is not None, "INVALID_INPUT",
                f"source={source} requires note: say which gate steps ran and which did not"
                + (" (and when this record was filled in later)" if source == "backfill" else ""))
        require(reviewed_sha != UNVERIFIED, "INVALID_INPUT",
                f"source={source} needs a recorded head_sha to verify; `{UNVERIFIED}` means there is none")
        root = conn.execute("SELECT root FROM projects WHERE id=?", (ticket["project_id"],)).fetchone()[0]
        branch = text(data, "target_branch", 200)
        command = ancestry_proof(root, reviewed_sha, branch)
        proc = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor",
                               reviewed_sha, branch], capture_output=True, text=True)
        require(proc.returncode == 0, "NOT_LANDED",
                f"{ticket['id']} 的提交 {reviewed_sha} 不在 {branch} 里（{command} 退出 {proc.returncode}）"
                f"：这条合并记录不能补，先查清它到底落到哪里")
        return source, note, command, now()

    def _repair_backfill_command(self, conn, data):
        r"""只改补记行 `verified_by` 的路径写法，命令本身一个字不许变（AW-116）。

        为什么需要这一步：第一版把库里的 Windows 路径原样写进了命令（`d:\cindy-...`），
        抄进 Git Bash 时反斜杠会被当转义吃掉，命令跑不通。这里只允许「同一命令、另一种
        路径写法」这一种差异：把存量值与新写的规范值都按正斜杠归一后比对，不相等就拒
        并让人先读那一行——不做模糊替换，也不趁这次机会改别的字段。
        """
        ticket = self._ticket(conn, data)
        row = conn.execute("SELECT * FROM merges WHERE ticket_id=?", (ticket["id"],)).fetchone()
        require(row is not None, "NOT_FOUND", "This ticket has no merge record to repair")
        require(row["source"] == "backfill", "INVALID_STATE",
                "Only a backfill record carries a command written by this tool")
        root = conn.execute("SELECT root FROM projects WHERE id=?", (ticket["project_id"],)).fetchone()[0]
        canonical = ancestry_proof(root, row["reviewed_sha"], row["target_branch"])
        stored = row["verified_by"] or ""
        require(stored.replace("\\", "/") == canonical, "COMMAND_MISMATCH",
                "The stored verified_by is not this command with a different path spelling; "
                "read the row before rewriting it")
        if stored == canonical:
            return {"ticket_id": ticket["id"], "changed": False, "verified_by": canonical}
        conn.execute("UPDATE merges SET verified_by=? WHERE id=?", (canonical, row["id"]))
        return {"ticket_id": ticket["id"], "changed": True, "before": stored, "verified_by": canonical}

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
        #
        # `since_days` 只是收窄窗口，不改语义：给 1 就只列最近一天的，不给就是全量。
        # 它存在的原因是这张单的历史行越积越多（17 张在 master 上做的老单永远合不了），
        # 合并时人要看的是「最近验收完的那几张」。不传时 SQL 与以前逐字一致。
        pid = None
        if "project_id" in data:
            pid = text(data, "project_id", 200)
        since = None
        if "since_days" in data:
            days = integer(data, "since_days", 1, 1, 365)
            since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = conn.execute(
            "SELECT t.id,t.project_id,t.title,t.state,t.revision,t.created_at FROM tickets t"
            " LEFT JOIN merges m ON m.ticket_id=t.id"
            " WHERE t.state='done' AND m.id IS NULL AND (? IS NULL OR t.project_id=?)"
            " AND (? IS NULL OR t.created_at>=?)"
            " ORDER BY t.created_at,t.id", (pid, pid, since, since))
        items = [{**dict(row), "has_merge": False} for row in rows]
        return {"items": items, "count": len(items)}

    def _events(self, conn, data):
        ticket = self._ticket(conn, data)
        limit, offset = integer(data, "limit", 50, 1), integer(data, "offset", 0, 0, 1000000)
        rows = conn.execute("SELECT * FROM events WHERE ticket_id=? ORDER BY id LIMIT ? OFFSET ?", (ticket["id"], limit, offset))
        items = [dict(row) for row in rows]
        for row in items:
            row["detail"] = json_column(row["detail"], "detail", f"event {row['id']}")
        return {"items": items, "limit": limit, "offset": offset}

    def _relocate_project(self, conn, data):
        """换根不改身份：`project_id`、发号盘与名下工单一律不动，只改 `projects.root`（AW-154）。

        改路径以前只能「`unregister_project` → `register_project`」，而前者对名下有工单的
        项目直接报 `PROJECT_HAS_TICKETS`——两条叠起来等于「有工单的项目永远改不了根」。
        2026-11-24 要把 heartopia-script-evolution 从 C: 搬到 D:，走的就是这条路。

        非法输入一律在写入之前拒，被拒时一行不写：空值与相对路径由 `project_root()` 判
        INVALID_INPUT，新根不存在判 FILE_NOT_FOUND，不存在的 project_id 由 `_project` 判
        NOT_FOUND，新根已经被另一个项目占着报 CONFLICT（root 是身份，一个目录只能有一个项目）。
        `projects.root` 带唯一约束，所以这一条在库面上也不会被绕过。
        新旧根相同不算迁移：回执 `relocated: false`，一行不写。

        为什么放在类末尾：文件末尾那段注释已经说过——在文件中间插函数会把 `PRD/` 与
        `AGENTS.md` 里 `core.py:<行号>` 的引用整体推走，ref_gate 会红。
        """
        project = self._project(conn, text(data, "project_id", 200))
        root = project_root(data.get("new_root"))
        owner = conn.execute("SELECT * FROM projects WHERE root=?", (root,)).fetchone()
        if owner is not None and owner["id"] != project["id"]:
            raise Problem("CONFLICT", f"Root {root} is already registered as project {owner['id']}; "
                                      "root is identity, so one directory cannot host two projects "
                                      "and neither record is changed")
        if root == project["root"]:
            return {"project_id": project["id"], "old_root": project["root"], "root": root,
                    "relocated": False}
        conn.execute("UPDATE projects SET root=? WHERE id=?", (root, project["id"]))
        return {"project_id": project["id"], "old_root": project["root"], "root": root,
                "relocated": True}


# 验收记录的三档（AW-132）。放在文件末尾是有原因的：`PRD/` 与 `AGENTS.md` 里有
# `task_center/core.py:<行号>` 形式的引用，ref_gate 会核「那一行非空」。在文件中间
# 插入函数会把它们整体推走，于是这单修的是验收记录，红的却是别人文档里的引用。
# `_get_ticket` 里那四行也是同一个理由：替换成同样的行数，不改它在文件里的位置。
CHECK_STATUSES = ("pass", "unverified", "fail")
# 旧写法 `passed` 与新三档的对应关系：true → pass，false → fail。
LEGACY_CHECK_STATUS = {True: "pass", False: "fail"}


def review_checks(criteria_items, checks):
    """把一条 review 的 checks 归一成三档：`pass` / `unverified` / `fail`。

    为什么要三档：库里 accept 曾经要求每条 check 都为 true，于是「真机判据无法执行」
    只能填 true 再在 reason 里写字，accept 与整体通过在库面上长得一样（CH-096）。
    现在 unverified 是独立的一档：不阻断 accept，但会被数进 checks_summary 与摘要行。

    两种写法都收：
    - 新写法 `{"id","status","evidence"}`，status 取 pass / unverified / fail；
    - 旧写法 `{"id","passed","evidence"}`，归一成 pass / fail。
    两个都给时不许自相矛盾；`unverified` 不许带 `passed`——未验既不是 true 也不是
    false，留一个布尔就会被下游读成其中之一。
    """
    expected = [item["id"] for item in criteria_items]
    require(isinstance(checks, list) and len(checks) == len(expected), "INVALID_INPUT",
            "Review must cover every acceptance criterion exactly once")
    seen, normalized = set(), []
    for check in checks:
        require(isinstance(check, dict), "INVALID_INPUT", "Each review check must be an object")
        allowed = {"id", "evidence", "status", "passed"}
        require(set(check) <= allowed and {"id", "evidence"} <= set(check), "INVALID_INPUT",
                "Check requires id and evidence, plus status (pass/unverified/fail) or passed")
        cid = text(check, "id", 100)
        evidence = text(check, "evidence")
        require(cid in expected and cid not in seen, "INVALID_INPUT", "Unknown or duplicate review check")
        status, passed = check.get("status"), check.get("passed")
        require(status is not None or passed is not None, "INVALID_INPUT",
                "Check needs status or passed; without either nobody said whether it was verified")
        if status is not None:
            require(status in CHECK_STATUSES, "INVALID_INPUT",
                    f"Check status must be pass, unverified or fail; got {status!r}")
        if passed is not None:
            require(type(passed) is bool, "INVALID_INPUT", "Check passed must be a boolean")
            require(status is None or LEGACY_CHECK_STATUS[passed] == status, "INVALID_INPUT",
                    "Check status and passed disagree")
            require(status != "unverified", "INVALID_INPUT",
                    "An unverified check cannot carry passed: it is neither pass nor fail")
            status = LEGACY_CHECK_STATUS[passed]
        seen.add(cid)
        item = {"id": cid, "status": status, "evidence": evidence}
        if status != "unverified":
            # 旧读者只认布尔：pass / fail 留一份镜像，未验故意不留。
            item["passed"] = status == "pass"
        normalized.append(item)
    return normalized


def stored_checks(raw_checks, warnings=None, where="review"):
    """读取时的兼容层：schema 16 以前的 review 只存了 `passed`，读出来补一个 status。

    这里不校验、不拒绝：历史行的形状是既成事实，`get_ticket` 的职责是把它读得出来，
    而不是让一条旧行把整张单的验收历史变成异常。真正的形状闸门在写入侧 `review_checks`。
    解不开时走 `json_column` 同一条路（AW-166）：原文留住，where / field / error 进 `decode_warnings`。
    """
    checks = json_column(raw_checks, "checks", where, warnings, as_list=True)
    for item in checks:
        if not isinstance(item, dict):
            continue
        if "status" not in item:
            item["status"] = LEGACY_CHECK_STATUS.get(item.get("passed"), "unverified")
    return checks


def checks_summary(checks):
    """按三档数一遍：`total` / `pass` / `unverified` / `fail`；读不出状态的那条按未验数。"""
    tally = {"total": len(checks), "pass": 0, "unverified": 0, "fail": 0}
    for item in checks:
        status = item.get("status") if isinstance(item, dict) else None
        tally[status if status in CHECK_STATUSES else "unverified"] += 1
    return tally


def review_summary_line(verdict, tally):
    """工单摘要上那一行：零的档位不写，所以「全部已验证」与「带未验」字面就不同。"""
    parts = []
    if tally["pass"]:
        parts.append(f"{tally['pass']} 条已验证")
    if tally["unverified"]:
        parts.append(f"{tally['unverified']} 条未验")
    if tally["fail"]:
        parts.append(f"{tally['fail']} 条失败")
    return f"{verdict}：" + ("、".join(parts) if parts else "0 条")


def review_snapshot(raw_checks, verdict, warnings=None, where="review"):
    """一条 review 的对外视图：checks 反序列化 + 三档计数 + 一行摘要。"""
    checks = stored_checks(raw_checks, warnings, where)
    tally = checks_summary(checks)
    return {"checks": checks, "checks_summary": tally, "summary": review_summary_line(verdict, tally)}


def json_column(raw, field, where, warnings=None, as_list=False):
    """把库里一列 JSON 读出来；读不出来就留住原文，并记下「哪一列、哪张单、什么异常」。

    AW-163 的现场：`HSE-007` 的一条提交把 `evidence` 存成了一句纯文本（早期版本没有形状
    闸门），`json.loads` 抛的 `JSONDecodeError` 是 `ValueError`，被 `execute()` 的兜底折成
    一句「Invalid input or inaccessible local path」——**这一张单从此整张读不出来**，
    而 `work_gate --list` 又按「一张单读不出来就整表不可读」判，于是整份在途清单一起瞎。
    历史行不会因为代码变好而自愈，所以这里定的是「读得出来多少给多少」：解不开就把原文
    留住，字段名、位置、异常类型与报文写进 `warnings`，谁能修就照着它去修。

    `as_list=True` 的列（`artifacts` / `evidence` / `risks` / `criteria` / `options` / `checks`）
    兜成一个只装原文的列表：读的人看到的是「这一列存着这段字」，不是一句空数组——空数组
    会把「这行是坏的」说成「这行没有内容」。`None` 与空白串兜成 `[]`，那是真的没有。
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return [] if as_list else None
    problem = None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        problem = {"error": type(exc).__name__, "detail": str(exc), "raw": str(raw)[:120]}
        value = raw
    if as_list and not isinstance(value, list):
        if problem is None:
            problem = {"error": f"NotAList({type(value).__name__})",
                       "detail": "这一列不是 JSON 数组", "raw": str(raw)[:120]}
        value = [value]
    if problem is not None and warnings is not None:
        warnings.append({**problem, "where": where, "field": field})
    return value


def decode_submission(item, warnings=None):
    """一条提交的四个 JSON 列就地解码（AW-163）；解不开的按 `json_column` 的口径留住原文。

    预演原样回给验收会话：NULL 仍然是 None（「没跑过／算不出来」），不是 `{"conflicts": []}`
    （「跑过且干净」）——两者在验收眼里是两件事（PRD-015 R2）。
    """
    for key in ("artifacts", "evidence", "risks"):
        item[key] = json_column(item[key], key, f"submission {item.get('id')}", warnings, as_list=True)
    item["conflict_preview"] = (None if item["conflict_preview"] is None
                                else json_column(item["conflict_preview"], "conflict_preview",
                                                 f"submission {item.get('id')}", warnings))


def unreadable_message(operation, exc):
    """`execute()` 兜底那一句：说清是哪个操作、什么异常、哪个路径。

    以前四种异常（ValueError / TypeError / OSError / RecursionError）共用一句
    「Invalid input or inaccessible local path」，读的人分不出「参数写错了」与「文件打不开」，
    只能猜。现在异常类型、报文与路径（OSError 带 filename 时）都写进来——定位得住，
    才谈得上修。
    """
    detail = f"{type(exc).__name__}: {exc}"
    path = getattr(exc, "filename", None)
    if path:
        detail += f"（path={path}）"
    return f"{operation}: Invalid input or inaccessible local path — {detail}"
