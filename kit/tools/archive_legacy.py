#!/usr/bin/env python
"""存量缺口的一次性清账：把 11-21 之前就验收过、却缺回执或缺合并记录的单归档。

为什么要有这个脚本：`wait_audit` 每天报 130 行（79 张 done 没回执、51 张 done 没合并），
去重后 85 张全是 11-21 之前的存量。它们没有 `head_sha`，而 `_reviewed_sha` 要求
「有 head_sha 才能比」、`_merge_provenance` 又要求「没有 head_sha 就不能补记录」——
两道门方向相反，所以补 `merges` 行这条路对它们根本走不通；把巡检的阈值调松更不行，
那是让闸门闭嘴而不是让事实说清。剩下的第三种做法是承认事实：它们**验收过**
（`reviews.verdict='accept'` 在库里），只是证据链断在 schema 8 之前。于是给它们一个
终态 `archived`——不进 `list_unmerged`、不被 `wait_audit` 的 done 两类命中、不算在途，
而 `reviews` / `submissions` 的历史行一条不删。

判据只有一处，写在这里（`LEGACY_CUTOFF` + `gaps_of()`）：

    state='done'
    且（`notices` 无行 或 `merges` 无行）
    且 `created_at` 早于 2026-11-21T00:00:00+00:00

日期界的理由是「11-21 是这套账开始按 schema 9 记 `head_sha` 的日子」：那之后验收的单，
缺记录是**流程没走完**（该补回执、该走合并），不是历史断链。所以界必须按创建时间判，
不能按「缺不缺记录」判——后者会把今天刚验收、正在等合并的单一起扫进来。

    python tools/archive_legacy.py --dry-run                     # 只读：列出来，数一共几张
    python tools/archive_legacy.py --apply                       # 逐张 archive_ticket，写 journal
    python tools/archive_legacy.py --dry-run --db <副本库>        # 演练：不碰主库
    python tools/archive_legacy.py --apply --db <副本库> --journal-dir <目录>

**`--apply` 只经 `archive_ticket` 写库，不写一行 SQL。** 这样每张单都留一条
`operation=archive_ticket` 的事件，「谁在什么时候因为什么归档的」事后查得到；直接
`UPDATE tickets SET state='archived'` 会得到同样的状态和一份查不回来的账。`revision`
不变（归档不是新的一代），所以重跑 `--apply` 只会剩下 0 张候选，不会重复归档。

退出码：0 = 干完了（`--dry-run` 有候选也算 0：它本来就只是列清单）；1 = 有单归档失败
（journal 照写，失败原因在 `failures` 里）；2 = 参数或库有问题（缺库打印 `DB_NOT_FOUND`）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
# 两处都要进 sys.path：`tools/` 给 `wait_audit` 的同一套库查找与只读打开，仓库根给
# `task_center`。少后一条，在 `.worktrees/AW-xxx/tools/` 里跑就 import 不到中心库代码。
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

from wait_audit import AuditError, default_ticket_db, parse_time, read_only  # noqa: E402

# 日期界：schema 9 之后 `submissions.head_sha` 才开始记，那之前验收的单缺记录属历史断链。
LEGACY_CUTOFF = "2026-11-21T00:00:00+00:00"
# journal 的默认落点与建单库同目录：副本库演练时自然落到副本旁边，不污染主库的账。
JOURNAL_DIRNAME = "archive-journal"
REASON = ("存量缺口清账（AW-128）：11-21 前已验收，但库里缺回执或合并记录，"
          "按历史事实归档为 archived；不补 merges 行，历史行一条不删")
GAP_NOTICE = "no_notice"
GAP_MERGE = "no_merge"
GAP_LABELS = {GAP_NOTICE: "done 没回执", GAP_MERGE: "done 没合并"}


def gaps_of(notices_n: int, merges_n: int) -> list[str]:
    """这张单的缺口：两类各自独立，两类都缺就是两条。"""
    gaps = []
    if notices_n == 0:
        gaps.append(GAP_NOTICE)
    if merges_n == 0:
        gaps.append(GAP_MERGE)
    return gaps


def legacy_rows(conn: sqlite3.Connection, cutoff=LEGACY_CUTOFF) -> list[dict]:
    """判据唯一实现：`done` + 有缺口 + `created_at` 早于日期界。"""
    bound = parse_time(cutoff)
    if bound is None:
        raise AuditError(f"日期界解不开：{cutoff}")
    try:
        rows = conn.execute(
            "SELECT t.id, t.project_id, t.state, t.title, t.created_at,"
            " (SELECT COUNT(*) FROM notices n WHERE n.ticket_id=t.id) AS notices_n,"
            " (SELECT COUNT(*) FROM merges m WHERE m.ticket_id=t.id) AS merges_n"
            " FROM tickets t WHERE t.state='done' ORDER BY t.created_at, t.id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise AuditError(f"SCHEMA_MISMATCH：读不了 tickets / notices / merges（{exc}）") from exc
    picked = []
    for row in rows:
        gaps = gaps_of(row["notices_n"], row["merges_n"])
        if not gaps:
            continue
        created = parse_time(row["created_at"])
        if created is None:
            # 时间解不开就不是「早于日期界」：不猜，留给下一次有人来看。
            continue
        if created >= bound:
            continue
        picked.append({
            "ticket_id": row["id"], "project_id": row["project_id"], "title": row["title"] or "",
            "created_at": row["created_at"], "gaps": gaps,
            "notices": row["notices_n"], "merges": row["merges_n"],
        })
    return picked


def render(rows: list[dict], cutoff: str, db: Path, applying: bool) -> str:
    lines = [f"库：{db}", f"判据：state=done 且有缺口 且 created_at 早于 {cutoff}"]
    for row in rows:
        gaps = "、".join(GAP_LABELS[gap] for gap in row["gaps"])
        lines.append(f"  {row['ticket_id']}\t{row['created_at']}\t{row['title']}\t缺口：{gaps}")
    verb = "将归档" if applying else "候选"
    lines.append(f"合计：{len(rows)} 张{verb}")
    return "\n".join(lines)


def archive_rows(db: Path, rows: list[dict]) -> dict:
    """逐张调 `archive_ticket`：每张都要现取 `expected_revision`，不抄 dry-run 时的值。"""
    from task_center import Center  # 放在函数里：--dry-run 不必加载写入侧

    center = Center(db)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archived, failures = [], []
    for row in rows:
        ticket_id, project = row["ticket_id"], row["project_id"]
        run = uuid4().hex[:8]
        current = center.execute("get_ticket", {"project_id": project, "ticket_id": ticket_id},
                                 request_id=f"archive-legacy-read-{ticket_id}-{run}")
        if not current["ok"]:
            failures.append({"ticket_id": ticket_id, "code": current["error"]["code"],
                             "message": current["error"]["message"]})
            continue
        reply = center.execute("archive_ticket", {
            "project_id": project, "ticket_id": ticket_id,
            "expected_revision": current["result"]["revision"], "reason": REASON,
        }, request_id=f"archive-legacy-{stamp}-{ticket_id}-{run}")
        if reply["ok"]:
            archived.append({**row, "state": reply["result"]["state"],
                             "revision": reply["result"]["revision"]})
        else:
            failures.append({"ticket_id": ticket_id, "code": reply["error"]["code"],
                             "message": reply["error"]["message"]})
    return {"archived": archived, "failures": failures}


def write_journal(journal_dir: Path, db: Path, rows: list[dict], result: dict,
                  cutoff: str) -> Path:
    journal_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = journal_dir / f"archive-legacy-{stamp}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db": str(db.resolve()),
        "cutoff": cutoff,
        "criterion": "state='done' 且（notices 无行 或 merges 无行）且 created_at 早于 cutoff",
        "reason": REASON,
        "ids": [row["ticket_id"] for row in result["archived"]],
        "count": len(result["archived"]),
        "tickets": result["archived"],
        "failures": result["failures"],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="archive_legacy.py",
        description="把 11-21 前已验收、却缺回执/合并记录的存量单归档为 archived（AW-128）。",
        epilog="退出码：0 干完了；1 有单归档失败；2 参数或库有问题。",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只列候选，一行不写（默认）")
    mode.add_argument("--apply", action="store_true", help="逐张 archive_ticket 并写 journal")
    parser.add_argument("--db", default=None, metavar="PATH",
                        help=f"工单中心库；默认 {default_ticket_db()}")
    parser.add_argument("--journal-dir", default=None, metavar="DIR",
                        help=f"journal 落点；默认 <库所在目录>/{JOURNAL_DIRNAME}")
    parser.add_argument("--cutoff", default=LEGACY_CUTOFF, metavar="ISO",
                        help=f"日期界（默认 {LEGACY_CUTOFF}）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:  # Windows 控制台默认 GBK，中文一打印就 UnicodeEncodeError
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    args = parse_args(sys.argv[1:] if argv is None else argv)
    db = Path(args.db).resolve() if args.db else default_ticket_db()
    journal_dir = Path(args.journal_dir) if args.journal_dir else db.parent / JOURNAL_DIRNAME
    applying = bool(args.apply)

    conn = None
    try:
        conn = read_only(db)
        rows = legacy_rows(conn, args.cutoff)
    except AuditError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()

    print(render(rows, args.cutoff, db, applying))
    if not applying:
        print("--dry-run：一行没写。要真归档就重跑，把 --dry-run 换成 --apply。")
        return 0
    if not rows:
        print("没有候选：这一份库的存量已经清过。journal 不写——写一份空账只会让「最新那份」变成零。")
        return 0

    result = archive_rows(db, rows)
    path = write_journal(journal_dir, db, rows, result, args.cutoff)
    print(f"归档完成：{len(result['archived'])} 张；失败 {len(result['failures'])} 张")
    print(f"journal：{path}")
    for failure in result["failures"]:
        print(f"  ! {failure['ticket_id']}：{failure['code']} {failure['message']}", file=sys.stderr)
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
