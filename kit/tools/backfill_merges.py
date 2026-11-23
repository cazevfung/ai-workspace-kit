"""把「已验收但没有合并记录」的单按 git 证据分档：能验证的补、不能验证的豁免。

用法（仓库根 {{WORKSPACE}}）：

    python tools/backfill_merges.py --dry-run
    python tools/backfill_merges.py --apply

三档的判据只有两个现场事实：这张单最新一条提交有没有 `head_sha`，以及
`git merge-base --is-ancestor <head_sha> master` 的退出码。

| 档 | 判据 | 处置 |
| --- | --- | --- |
| A | 有 `head_sha`，且它是 `master` 的祖先 | `--apply` 写一条 `merges` 行（`source=backfill`） |
| B | 没有 `head_sha`（schema 9 以前的提交） | 一行不写，落进豁免清单并写清理由 |
| C | 有 `head_sha`，但它不在 `master` 里 | 拒绝并点名，不补记——它到底落到哪里要先查清 |

为什么分三档而不是「补上就完了」：B 档没有任何可核对的树，给它补一条记录等于把
「没人记过」写成「已经合了」；C 档恰恰相反，它说明这张单**没有**进目标分支，给
它补记录就是把失败粉饰成成功。两种都是假值，都不写。

A 档的每一行由 `merge_ticket` 的 `source=backfill` 路径写入：它会当场重跑一次
`git merge-base --is-ancestor`，把真跑过的命令记进 `verified_by`，所以后人可以原样
重跑核对。补记之前若这张单一条回执都没有，先补一条 `failed`——合并发生在回执机制
之前是事实，`sent` 才是假的。

`--apply` 同时把 B 档落成豁免清单：`docs/unmerged-exemptions.json`（机器读）与
`docs/unmerged-exemptions.md`（人读）。`tools/auto_merge.py --list` 按这份清单分栏。
**B 档为空（乃至一张待补记的单都没有）时也照写一份空清单**：这份清单回答的是「现在
还有几张要豁免」，空是一等结果，不写就等于让上一次的快照继续冒充现状（AW-130）。

退出码：0 = 没有需要处理或全部处理完；1 = A 档有补记失败；2 = 有 C 档（没落地）。
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

import auto_merge  # noqa: E402

TARGET_BRANCH = "master"
DEFAULT_EXEMPTIONS_JSON = REPO / "docs" / "unmerged-exemptions.json"
DEFAULT_EXEMPTIONS_MD = REPO / "docs" / "unmerged-exemptions.md"

BUCKET_A = "A"
BUCKET_B = "B"
BUCKET_C = "C"

REASON_NO_HEAD_SHA = "no_head_sha"
REASON_CANNOT_CHECK = "cannot_check"

# 空清单到底是什么意思。这句话两个文件都写、`auto_merge.py --list` 也照它说，
# 否则读到空清单的人会以为「以前那 51 张被人抹掉了」（AW-130）。
EMPTY_MEANS = (
    "此刻没有「state=done 但没有 merges 行、又没有 head_sha」的单。以前列在这里的历史单只有"
    "两种去向：证据补齐了（A 档补了 merges），或者已被 archived 终态覆盖 —— archived 不算 "
    "done，既不需要也不允许补合并记录。所以读到空清单就是答案，不必再去翻旧版本找它们。"
)


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def git(root, *args):
    """跑一次 git；调用方自己判退出码（这里不抛，避免把「不可核对」变成崩溃）。"""
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def ancestry_command(root, sha, branch=TARGET_BRANCH):
    # 与 `task_center.core.ancestry_proof` 同一个写法：正斜杠，三种壳里都能原样跑。
    shown = str(root).replace("\\", "/")
    if " " in shown:
        shown = f'"{shown}"'
    return f"git -C {shown} merge-base --is-ancestor {sha} {branch}"


def latest_head_sha(center, project_id, ticket_id):
    reply = center.execute("get_ticket", {"project_id": project_id, "ticket_id": ticket_id})
    if not reply["ok"]:
        return None
    submissions = reply["result"]["submissions"]
    return submissions[-1]["head_sha"] if submissions else None


def find_merge_commit(root, sha, branch=TARGET_BRANCH):
    """找出把 `sha` 第一次带进目标分支的那次合并提交；没有就返回 (sha, 'direct')。

    `--no-ff` 合进来的分支，合并提交的第二个父就是被合的那棵树，所以按 parent2 == sha
    认；快进或直接在 master 上做的单没有单独的合并提交，此时记它自己的 sha，并在 note
    里写明这一点——写一个不存在的合并提交才是编。
    """
    proc = git(root, "rev-list", "--merges", "--reverse", "--ancestry-path",
               f"{sha}..{branch}")
    if proc.returncode != 0:
        return sha, "direct"
    for candidate in proc.stdout.split():
        parents = git(root, "rev-list", "--parents", "-n1", candidate)
        if parents.returncode != 0:
            continue
        parts = parents.stdout.split()
        if len(parts) == 3 and parts[2] == sha:
            return candidate, "merge"
    return sha, "direct"


def classify(center, item, branch=TARGET_BRANCH):
    """把一张 unmerged 单放进 A / B / C，并留下判据与可重跑的命令。"""
    project_id, ticket_id = item["project_id"], item["id"]
    sha = latest_head_sha(center, project_id, ticket_id)
    entry = {"ticket_id": ticket_id, "project_id": project_id, "title": item.get("title", ""),
             "head_sha": sha, "merge_commit": None, "note_kind": None}
    if not sha:
        entry.update(bucket=BUCKET_B, reason=REASON_NO_HEAD_SHA,
                     reason_text="最新提交没有 head_sha（schema 9 以前的提交），没有可核对的树")
        return entry
    root = center.execute("list_projects", {})
    root = next((row["root"] for row in root["result"]["items"] if row["project_id"] == project_id), None)
    command = ancestry_command(root, sha, branch)
    proc = git(root, "merge-base", "--is-ancestor", sha, branch)
    if proc.returncode == 0:
        commit, note_kind = find_merge_commit(root, sha, branch)
        entry.update(bucket=BUCKET_A, reason="landed", reason_text="head_sha 是 master 的祖先",
                     evidence=command, merge_commit=commit, note_kind=note_kind)
        return entry
    if proc.returncode == 1:
        entry.update(bucket=BUCKET_C, reason="NOT_LANDED", evidence=command,
                     reason_text=f"head_sha 不在 {branch} 里（{command} 退出 1）")
        return entry
    entry.update(bucket=BUCKET_B, reason=REASON_CANNOT_CHECK, evidence=command,
                 reason_text=f"核不出来：{command} 退出 {proc.returncode}"
                             + (f"（{(proc.stderr or '').strip()}）" if proc.stderr.strip() else ""))
    return entry


def ensure_notice(center, entry, when):
    """补记之前先保证这张单有一条回执记录：没有就补一条 `failed`，绝不写 `sent`。"""
    reply = center.execute("get_ticket", {"project_id": entry["project_id"],
                                          "ticket_id": entry["ticket_id"]})
    ticket = reply["result"]
    if ticket["notices"]:
        return 0
    owner = ticket.get("owner_session_id")
    if not owner:
        raise RuntimeError("这张单没有 owner_session_id，补记回执没有可写的收件会话")
    attempt = ticket["attempts"][-1] if ticket["attempts"] else None
    if attempt is None:
        raise RuntimeError("这张单没有任何执行尝试，补记回执没有可挂的 attempt")
    detail = (f"补记于 {when}（tools/backfill_merges.py）：这张单合并时还没有回执机制，"
              f"库里一条回执都没有，当时发了什么也无从考证。这条 failed 是补记记录，"
              f"不是当时发送的记录；{owner} 是那一代的协调会话（owner）。")
    written = center.execute("record_notice", {
        "project_id": entry["project_id"], "ticket_id": entry["ticket_id"],
        "attempt_id": attempt["id"], "session_id": owner, "origin_session_id": owner,
        "status": "failed", "detail": detail, "target_session_id": owner,
        "wake_kind": "unknown",
    }, f"aw116-notice-{entry['ticket_id']}-{uuid4().hex[:8]}")
    if not written["ok"]:
        error = written["error"]
        raise RuntimeError(f"补记回执被拒：{error['code']} {error['message']}")
    return 1


def record_merge(center, entry, when):
    """给 A 档写一条补记的合并记录；一切核对都在 `merge_ticket` 里当场做。"""
    if entry["note_kind"] == "merge":
        note = (f"补记于 {when}（tools/backfill_merges.py）：这张单当时合进了 {TARGET_BRANCH}，"
                f"但那时还没有合并记录。commit 记的是把 {entry['head_sha'][:8]} 带进目标分支的"
                f"那次合并提交；原合并时的测试结论没有留下，tests 一栏如实写「未记录」。")
    else:
        note = (f"补记于 {when}（tools/backfill_merges.py）：这张单的树已在 {TARGET_BRANCH} 里，"
                f"但没有单独的合并提交（快进或直接在目标分支上做）。commit 记的是被验收的那棵树"
                f"本身的 sha；原合并时的测试结论没有留下，tests 一栏如实写「未记录」。")
    payload = {
        "project_id": entry["project_id"], "ticket_id": entry["ticket_id"],
        "commit": entry["merge_commit"] or entry["head_sha"],
        "target_branch": TARGET_BRANCH,
        "tests": "未记录（补记：原合并时的测试结论无从考证）",
        "reviewed_sha": entry["head_sha"], "source": "backfill", "note": note,
    }
    written = center.execute("merge_ticket", payload,
                             f"aw116-backfill-{entry['ticket_id']}-{uuid4().hex[:8]}")
    if not written["ok"]:
        error = written["error"]
        raise RuntimeError(f"{error['code']}：{error['message']}")
    return written["result"]


def write_exemptions(entries, path_json, path_md, when):
    """把 B 档落成两个文件：同一份事实，一份给机器、一份给人。

    **空清单也要写。** 这份清单回答的是「现在还有几张要豁免」，空是一等结果；
    不写就等于让上一次的快照继续冒充现状——2026-11-22 那份列着 51 张、而它们
    早已是 archived 的单，就是这么留下来的（AW-130）。
    """
    payload = {
        "generated_at": when,
        "generated_by": "tools/backfill_merges.py",
        "target_branch": TARGET_BRANCH,
        "pending": len(entries),
        "empty_means": EMPTY_MEANS,
        "reason": ("这些单的「已验收但没有合并记录」没有可核对的证据（最新提交没有 head_sha），"
                   "补一条记录等于把没人记过写成已经合了。它们不是等待合并的单。"),
        "check_command": "python tools/backfill_merges.py --dry-run",
        "entries": [
            {"ticket_id": entry["ticket_id"], "project_id": entry["project_id"],
             "title": entry["title"], "reason": entry["reason"],
             "reason_text": entry["reason_text"], "checked_at": when}
            for entry in entries
        ],
    }
    path_json.parent.mkdir(parents=True, exist_ok=True)
    path_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# 历史合并记录豁免清单",
        "",
        f"生成时间：{when}　生成命令：`python tools/backfill_merges.py --apply`",
        "",
        "这份清单回答的是「`done` 但没有 `merges` 行」里哪些**不该**补记录。判断依据不是",
        "猜测，而是两个现场事实：最新提交有没有 `head_sha`，以及它在不在 `master` 里。",
        "",
        "- **A 档（可验证）**：有 `head_sha` 且是 `master` 的祖先 → 由本工具补一条",
        "  `source=backfill` 的 `merges` 行，不进这份清单。",
        "- **B 档（本清单）**：最新提交没有 `head_sha`（schema 9 以前的提交）→ 没有可核对的树，",
        "  一行不写，在这儿写清理由。它们不是等待合并的单。",
        "- **C 档（不豁免）**：有 `head_sha` 但不在 `master` 里 → 说明它没落地，工具拒绝并点名；",
        "  这类单留在 `auto_merge.py --list` 的「真待合并」栏里等人判断。",
        "",
        f"本次豁免 {len(entries)} 张。核对用：`python tools/backfill_merges.py --dry-run`。",
        "",
    ]
    if entries:
        lines += [
            "| 工单 | 项目 | 标题 | 原因 | 核对时间 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for entry in entries:
            title = str(entry["title"]).replace("|", "\\|")
            lines.append(f"| {entry['ticket_id']} | {entry['project_id']} | {title} | "
                         f"{entry['reason']}：{entry['reason_text']} | {when} |")
    else:
        lines += ["**清单为空是什么意思：**", "", EMPTY_MEANS]
    lines.append("")
    path_md.write_text("\n".join(lines), encoding="utf-8")


def print_buckets(entries, out):
    titles = {
        BUCKET_A: "A 档（可验证：有 head_sha 且在 master 里）",
        BUCKET_B: "B 档（没有可核对的证据：最新提交没有 head_sha）",
        BUCKET_C: "C 档（有 head_sha 但不在 master 里：不补记，先查清落到哪里）",
    }
    for bucket in (BUCKET_A, BUCKET_B, BUCKET_C):
        members = [entry for entry in entries if entry["bucket"] == bucket]
        out(f"{titles[bucket]} {len(members)} 张")
        for entry in members:
            head = (entry["head_sha"] or "(无 head_sha)")[:12]
            detail = f"  {entry['ticket_id']}  head={head}"
            if bucket == BUCKET_A:
                detail += (f"  合并提交={entry['merge_commit'][:12]}（{entry['note_kind']}）"
                           f"  核对命令：{entry['evidence']}")
            else:
                detail += f"  原因：{entry['reason']} —— {entry['reason_text']}"
            out(detail)
    counts = {bucket: len([e for e in entries if e["bucket"] == bucket]) for bucket in (BUCKET_A, BUCKET_B, BUCKET_C)}
    out(f"合计 {len(entries)} 张：A={counts[BUCKET_A]}，B={counts[BUCKET_B]}，C={counts[BUCKET_C]}")
    return counts


def run(center, dry_run, exemptions_json=DEFAULT_EXEMPTIONS_JSON,
        exemptions_md=DEFAULT_EXEMPTIONS_MD, out=print, branch=TARGET_BRANCH):
    reply = center.execute("list_unmerged", {})
    if not reply["ok"]:
        out(f"list_unmerged 失败：{reply['error']['code']} {reply['error']['message']}")
        return 1
    items = reply["result"]["items"]
    if not items:
        out("没有「已验收但没有合并记录」的工单，不需要补记。")
        if dry_run:
            out("这是预演：库、豁免清单与合并记录都没有改动。")
            return 0
        # 一张都不用补记，不等于「什么都不写」：清单要落成空的一版，否则上一次的
        # 快照会继续冒充现状（AW-130）。
        when = stamp()
        write_exemptions([], Path(exemptions_json), Path(exemptions_md), when)
        out(f"豁免清单已刷新为空清单：{exemptions_json} / {exemptions_md}（0 张）")
        return 0
    entries = [classify(center, item, branch) for item in items]
    counts = print_buckets(entries, out)
    if dry_run:
        out("这是预演：库、豁免清单与合并记录都没有改动。")
        return 0

    when = stamp()
    written = notices = 0
    failures = 0
    for entry in entries:
        if entry["bucket"] != BUCKET_A:
            continue
        try:
            notices += ensure_notice(center, entry, when)
            record_merge(center, entry, when)
            written += 1
            out(f"  已补记 {entry['ticket_id']}（commit {entry['merge_commit']}）")
        except Exception as exc:  # noqa: BLE001 — 一张失败不该让整批停住，账要照记
            failures += 1
            out(f"  补记失败 {entry['ticket_id']}：{exc}")

    exempted = [entry for entry in entries if entry["bucket"] == BUCKET_B]
    # B 档为空也写：空清单是本工具的一个正常结果，不是「不用管」。
    write_exemptions(exempted, Path(exemptions_json), Path(exemptions_md), when)
    if exempted:
        out(f"豁免清单已写：{exemptions_json} / {exemptions_md}（{len(exempted)} 张）")
    else:
        out(f"B 档为空：豁免清单已刷新为空清单（{exemptions_json} / {exemptions_md}，0 张）")

    out(f"补记 {written} 张，补回执 {notices} 条，失败 {failures} 张；"
        f"C 档 {counts[BUCKET_C]} 张未处理（它没落地，不补记）。")
    if counts[BUCKET_C]:
        return 2
    return 1 if failures else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="历史合并记录后补：能验证的补、不能验证的豁免")
    parser.add_argument("--dry-run", action="store_true", help="只打印三档清单，不写库、不写豁免清单")
    parser.add_argument("--apply", action="store_true", help="给 A 档写合并记录与必要的 failed 回执，并落豁免清单")
    parser.add_argument("--db", default=None, help="中心库路径，默认按 center.json 发现")
    parser.add_argument("--exemptions-json", default=str(DEFAULT_EXEMPTIONS_JSON))
    parser.add_argument("--exemptions-md", default=str(DEFAULT_EXEMPTIONS_MD))
    args = parser.parse_args(argv)
    if args.dry_run == args.apply:
        print("要选一个：--dry-run 只打印，--apply 才写库。")
        return 2
    center, _ = auto_merge.open_center(args.db)
    return run(center, dry_run=args.dry_run, exemptions_json=args.exemptions_json,
               exemptions_md=args.exemptions_md)


if __name__ == "__main__":
    sys.exit(main())
