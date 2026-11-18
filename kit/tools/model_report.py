#!/usr/bin/env python
"""模型对照表：一条命令把「哪张单用了哪个模型、花了多少、返工几次」列出来。

以前这张表是一次性写出来的查询，换个人就复现不出来——不知道查了哪个库、
哪个 Cindy 库、成交口径按什么算。所以把口径写进脚本，零参数可用：

    python tools/model_report.py
    python tools/model_report.py --db <中心库> --cindy-db <Cindy 主库>

只读 join 两个库：
- 中心库：工单 / 尝试 / 提交 / 验收（默认仓库内 .data/task-center.sqlite3）
- Cindy 主库：会话的模型、effort、花费、token、起止时间
  （默认 %APPDATA%/Cindy 下 mtime 最新的 cindy-*.db，并把实际用的那个打印出来）

口径（写在这里，不靠口头约定，改口径请改这一段）：

1. 尝试数 = 该工单 attempts 的条数。
2. 验收轮次 = 经 submissions 关联到该工单 attempts 的 reviews 条数。
   一张单可能被 review 多次，review 不是尝试，不能混。
3. 累计花费 / 累计 token / 累计墙钟 = 该工单所有**尝试会话**求和。
   墙钟 = last_activity_at - created_at，单位分钟。
   **按会话去重再求和**：同一张单的多次尝试可能落在同一条会话上
   （AW-038 五个 attempt 共用一条会话），逐尝试求和会把同一笔花费算五遍。
   这也是「多次 review 不会把花费算多遍」的机制所在。
4. 一次通过 = 该单只有一次 review 且 verdict 是 accept；否则算返工（被退过）。
   一次 review 都没有的单不属于「返工」，单列「未验收」——它还没被判过。
5. 模型与 effort 取执行会话（attempts.session_id 对应的那条 sessions 记录）。
   同一张单不同尝试用了不同模型时，模型列写成井号分隔的集合，不挑其中一个；
   汇总也按这整个集合归桶，否则一张跨模型的单会被数两次。

两个库都以 sqlite3 的 `mode=ro` URI 打开并设 `PRAGMA query_only`，
跑完不写任何数据、不新建文件；库不存在就报错退出，不会顺手建一个空库。

退出码：0 出表；2 库缺失或表不对（原因写 stderr）。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ONE_PASS = "一次通过"
REWORK = "返工"
UNREVIEWED = "未验收"

NO_SESSION = "（无会话）"
MISSING_SESSION = "（会话缺失）"

REQUIRED_CENTER_TABLES = ("tickets", "attempts", "submissions", "reviews")
REQUIRED_CINDY_TABLES = ("sessions",)


class ReportError(RuntimeError):
    """报告生成不了时的唯一异常：带一句人能读的原因，由 main 打到 stderr。"""


# ---------------------------------------------------------------- 只读打开

def _connect_ro(uri: str, path: Path, label: str) -> sqlite3.Connection:
    """按已拼好的只读 URI 建连接，并核对路径与表结构。"""
    if not path.is_file():
        raise ReportError(f"{label}不存在：{path}（本脚本只读，不会新建任何库）")
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:  # 权限、不是 sqlite 文件等
        raise ReportError(f"{label}打不开：{path}（{exc}）") from exc
    return conn


def open_center_db(path) -> sqlite3.Connection:
    """中心库（工单 / 尝试 / 提交 / 验收）的只读连接。"""
    target = Path(path).expanduser().resolve()
    conn = _connect_ro(f"file:{target.as_posix()}?mode=ro", target, "中心库")
    _checked(conn, "中心库", target, REQUIRED_CENTER_TABLES)
    return conn


def open_cindy_db(path) -> sqlite3.Connection:
    """Cindy 主库（sessions：模型 / effort / 花费 / token / 起止时间）的只读连接。"""
    target = Path(path).expanduser().resolve()
    conn = _connect_ro(f"file:{target.as_posix()}?mode=ro", target, "Cindy 库")
    _checked(conn, "Cindy 库", target, REQUIRED_CINDY_TABLES)
    return conn


def _checked(conn, label: str, path: Path, names) -> None:
    """表不全就报错，并把刚建好的连接关掉——不然 Windows 上库文件被占着删不掉。"""
    try:
        _require_tables(conn, label, path, names)
    except Exception:
        conn.close()
        raise


def _require_tables(conn, label: str, path: Path, names) -> None:
    have = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
    missing = [name for name in names if name not in have]
    if missing:
        raise ReportError(f"{label}缺表：{'、'.join(missing)}（{path}）")


# ---------------------------------------------------------------- 库路径

def default_center_db(start=None) -> Path:
    """中心库默认位置：从脚本所在检出往上最近的那份 .data/task-center.sqlite3。

    为什么往上找而不是钉在脚本旁边：工单在 .worktrees/<工单号>/ 里干活，
    脚本在子树里，而 .data 属于主检出（工作树不带 .data）。按「最近的一份」取，
    主检出与工单工作树里零参数跑出来的是同一个库，不会因为换目录就换数据源。
    start 只是给测试用的入口，默认就是本脚本所在的检出根。
    """
    here = Path(start).resolve() if start is not None else Path(__file__).resolve().parent.parent
    for base in (here, *here.parents):
        candidate = base / ".data" / "task-center.sqlite3"
        if candidate.is_file():
            return candidate
    return here / ".data" / "task-center.sqlite3"


def cindy_dir() -> Path:
    """Cindy 主库所在目录：从环境变量拼，不写死用户名。"""
    roaming = os.environ.get("APPDATA")
    if not roaming:
        roaming = str(Path.home() / "AppData" / "Roaming")
    return Path(roaming) / "Cindy"


def pick_cindy_db(directory: Path | None = None) -> tuple[Path, list[Path]]:
    """在 Cindy 目录下取 mtime 最新的 cindy-*.db，返回 (选中的库, 全部候选)。

    多个库时选哪个不是随便定的：表里的会话可能分散在多个库，取最新的那个
    是当前在写的库。把候选数一起返回，输出里要打印出来，否则表对不上时无从追。
    """
    directory = cindy_dir() if directory is None else Path(directory)
    if not directory.is_dir():
        raise ReportError(f"Cindy 库目录不存在：{directory}（用 --cindy-db 指定库文件）")
    candidates = sorted(
        (p for p in directory.glob("cindy-*.db") if p.is_file()),
        key=lambda p: (p.stat().st_mtime, p.name),
    )
    if not candidates:
        raise ReportError(f"Cindy 库目录里没有 cindy-*.db：{directory}")
    return candidates[-1], candidates


# ---------------------------------------------------------------- 取数

@dataclass
class TicketRow:
    """一张工单在报告里的一行。"""

    ticket: str
    models: tuple[str, ...] = ()
    efforts: tuple[str, ...] = ()
    attempts: int = 0
    reviews: int = 0
    verdicts: tuple[str, ...] = ()
    cost: dict[str, float] = field(default_factory=dict)  # 币种 -> 金额，按会话去重后求和
    tokens: int = 0
    minutes: float = 0.0
    outcome: str = UNREVIEWED
    missing_sessions: tuple[str, ...] = ()

    @property
    def model_key(self) -> str:
        """汇总归桶用的模型标识：井号分隔的集合（不挑其中一个）。"""
        if self.models:
            return "#".join(self.models)
        return MISSING_SESSION if self.missing_sessions else NO_SESSION


def _ticket_ids(conn) -> list[str]:
    return [row[0] for row in conn.execute("select id from tickets")]


def _attempts_by_ticket(conn) -> dict[str, dict]:
    """工单 -> 尝试数与尝试会话（保持出现顺序，重复会话原样留着，后面去重）。"""
    out: dict[str, dict] = {}
    for ticket, session_id in conn.execute(
        "select ticket_id, session_id from attempts order by ticket_id, created_at, id"
    ):
        entry = out.setdefault(ticket, {"attempts": 0, "sessions": []})
        entry["attempts"] += 1
        if session_id:
            entry["sessions"].append(session_id)
    return out


def _verdicts_by_ticket(conn) -> dict[str, list[str]]:
    """工单 -> 验收 verdict 序列（按 review 时间）。只走 submissions 关联，不看 attempts 条数。"""
    out: dict[str, list[str]] = {}
    for ticket, verdict in conn.execute(
        """
        select a.ticket_id, r.verdict
        from reviews r
        join submissions s on s.id = r.submission_id
        join attempts a on a.id = s.attempt_id
        order by a.ticket_id, r.created_at, r.id
        """
    ):
        out.setdefault(ticket, []).append(verdict)
    return out


_SESSION_SQL = """
    select id, model, effort, total_token_usage, total_cost_amount,
           total_cost_currency, created_at, last_activity_at
    from sessions
    where id in ({placeholders})
"""


def _session_stats(conn, session_ids) -> dict[str, sqlite3.Row]:
    """批量取会话统计。分块是因为 sqlite 的占位符数量有上限。"""
    ids = sorted(set(session_ids))
    stats: dict[str, sqlite3.Row] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        sql = _SESSION_SQL.format(placeholders=",".join("?" * len(chunk)))
        for row in conn.execute(sql, chunk):
            stats[row[0]] = row
    return stats


def classify(verdicts) -> str:
    """一张单算不算一次通过：只有一次 review 且 accept 才算，其余见模块文档口径 4。"""
    verdicts = list(verdicts)
    if not verdicts:
        return UNREVIEWED
    if len(verdicts) == 1 and verdicts[0] == "accept":
        return ONE_PASS
    return REWORK


def _minutes(row: sqlite3.Row) -> float:
    """墙钟：last_activity_at 减 created_at，毫秒转分钟；缺一边记 0。"""
    started, ended = row["created_at"], row["last_activity_at"]
    if started is None or ended is None:
        return 0.0
    return max(0.0, (ended - started) / 60000.0)


@dataclass
class Report:
    center_db: Path
    cindy_db: Path
    cindy_candidates: list[Path]
    rows: list[TicketRow]
    warnings: list[str]
    currencies: tuple[str, ...]

    @property
    def summary(self) -> list[dict]:
        """按模型汇总：工单数、一次通过数、返工数（+ 未验收数，见口径 4）。"""
        buckets: dict[str, dict] = {}
        for row in self.rows:
            bucket = buckets.setdefault(
                row.model_key,
                {"model": row.model_key, "tickets": 0, ONE_PASS: 0, REWORK: 0, UNREVIEWED: 0,
                 "cost": {}, "tokens": 0},
            )
            bucket["tickets"] += 1
            bucket[row.outcome] += 1
            bucket["tokens"] += row.tokens
            for currency, amount in row.cost.items():
                bucket["cost"][currency] = bucket["cost"].get(currency, 0.0) + amount
        return sorted(buckets.values(), key=lambda b: (-b["tickets"], b["model"]))


def build_report(center_path, cindy_path, cindy_candidates=None) -> Report:
    """读两个库，产出按工单的行与按模型的汇总。全程只读。"""
    center_path = Path(center_path)
    cindy_path = Path(cindy_path)
    center = open_center_db(center_path)
    try:
        cindy = open_cindy_db(cindy_path)
    except Exception:
        center.close()
        raise
    try:
        tickets = _ticket_ids(center)
        attempts = _attempts_by_ticket(center)
        verdicts = _verdicts_by_ticket(center)
        all_sessions = [sid for entry in attempts.values() for sid in entry["sessions"]]
        stats = _session_stats(cindy, all_sessions)
    finally:
        center.close()
        cindy.close()

    rows: list[TicketRow] = []
    warnings: list[str] = []
    currencies: set[str] = set()

    for ticket in tickets:
        entry = attempts.get(ticket, {"attempts": 0, "sessions": []})
        seen: list[str] = []
        for sid in entry["sessions"]:
            if sid not in seen:
                seen.append(sid)
        verdict_of_ticket = verdicts.get(ticket, [])

        row = TicketRow(
            ticket=ticket,
            attempts=entry["attempts"],
            reviews=len(verdict_of_ticket),
            verdicts=tuple(verdict_of_ticket),
            outcome=classify(verdict_of_ticket),
        )

        models: set[str] = set()
        efforts: set[str] = set()
        for sid in seen:
            session = stats.get(sid)
            if session is None:
                row.missing_sessions += (sid,)
                continue
            if session["model"]:
                models.add(session["model"])
            if session["effort"]:
                efforts.add(session["effort"])
            row.tokens += session["total_token_usage"] or 0
            row.minutes += _minutes(session)
            amount = session["total_cost_amount"] or 0.0
            currency = session["total_cost_currency"] or ""
            row.cost[currency] = row.cost.get(currency, 0.0) + amount
            if currency:
                currencies.add(currency)

        row.models = tuple(sorted(models))
        row.efforts = tuple(sorted(efforts))
        if row.missing_sessions:
            warnings.append(
                f"{ticket}：{len(row.missing_sessions)} 条尝试的 session_id 在 Cindy 库里查不到，"
                "这一部分的花费与 token 记 0"
            )
        rows.append(row)

    rows.sort(key=ticket_sort_key)
    return Report(
        center_db=Path(center_path).resolve(),
        cindy_db=Path(cindy_path).resolve(),
        cindy_candidates=list(cindy_candidates or []),
        rows=rows,
        warnings=warnings,
        currencies=tuple(sorted(currencies)),
    )


def ticket_sort_key(row_or_id) -> tuple:
    """AW-9 排在 AW-10 前面：按前缀与数字排，数字相同再按原串。"""
    ticket = row_or_id.ticket if isinstance(row_or_id, TicketRow) else str(row_or_id)
    prefix, _, tail = ticket.rpartition("-")
    digits = ""
    for ch in tail:
        if not ch.isdigit():
            break
        digits += ch
    return (prefix, int(digits) if digits else -1, ticket)


# ---------------------------------------------------------------- 输出

def display_width(text: str) -> int:
    """中文字符占两列，否则表格在控制台里会歪。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int, align: str = "left") -> str:
    text = "" if text is None else str(text)
    gap = max(0, width - display_width(text))
    return " " * gap + text if align == "right" else text + " " * gap


def render_table(headers, aligns, rows) -> list[str]:
    widths = [display_width(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], display_width(str(cell)))
    lines = ["  ".join(_pad(h, widths[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(_pad(str(cell), widths[i], aligns[i]) for i, cell in enumerate(row)))
    return lines


def _cost_cells(cost: dict[str, float], single_currency: bool) -> str:
    if not cost:
        return "0.0000"
    parts = []
    for currency in sorted(cost):
        amount = cost[currency]
        parts.append(f"{amount:.4f}" if single_currency else f"{amount:.4f} {currency or '?'}")
    return " + ".join(parts)


def _fmt_int(value: int) -> str:
    return f"{value:,}"


def _fmt_minutes(minutes: float) -> str:
    return f"{minutes:.1f}"


def render(report: Report) -> str:
    single_currency = len(report.currencies) <= 1
    currency_label = report.currencies[0] if single_currency and report.currencies else ""
    cost_header = f"花费({currency_label})" if currency_label else "花费"

    lines: list[str] = []
    lines.append(f"中心库：{report.center_db}")
    if report.cindy_candidates:
        lines.append(
            f"Cindy 库：{report.cindy_db}"
            f"（目录下 {len(report.cindy_candidates)} 个 cindy-*.db，取 mtime 最新）"
        )
    else:
        lines.append(f"Cindy 库：{report.cindy_db}")
    if report.currencies and not single_currency:
        lines.append(f"注意：花费跨多个币种（{'、'.join(report.currencies)}），单元格内标注币种，不要直接相加")
    lines.append(
        f"工单 {len(report.rows)} 张；口径见脚本头部：尝试数=attempts 条数，"
        "验收轮次=经 submissions 关联的 reviews 条数，花费/token/墙钟按会话去重后求和"
    )
    lines.append("")

    headers = ["工单", "模型", "effort", "尝试", "验收轮次", cost_header, "token", "墙钟(分)", "结果"]
    aligns = ["left", "left", "left", "right", "right", "right", "right", "right", "left"]
    table_rows = [
        [
            row.ticket,
            "#".join(row.models) or (MISSING_SESSION if row.missing_sessions else NO_SESSION),
            "#".join(row.efforts) or "-",
            row.attempts,
            row.reviews,
            _cost_cells(row.cost, single_currency),
            _fmt_int(row.tokens),
            _fmt_minutes(row.minutes),
            row.outcome,
        ]
        for row in report.rows
    ]
    lines.extend(render_table(headers, aligns, table_rows))
    lines.append("")

    lines.append("按模型汇总")
    summary = report.summary
    summary_headers = ["模型", "工单数", "一次通过", "返工", "未验收", cost_header, "token"]
    summary_aligns = ["left", "right", "right", "right", "right", "right", "right"]
    summary_rows = [
        [
            bucket["model"],
            bucket["tickets"],
            bucket[ONE_PASS],
            bucket[REWORK],
            bucket[UNREVIEWED],
            _cost_cells(bucket["cost"], single_currency),
            _fmt_int(bucket["tokens"]),
        ]
        for bucket in summary
    ]
    lines.extend(render_table(summary_headers, summary_aligns, summary_rows))

    total_cost: dict[str, float] = {}
    for row in report.rows:
        for currency, amount in row.cost.items():
            total_cost[currency] = total_cost.get(currency, 0.0) + amount
    lines.append("")
    lines.append(
        f"合计：工单 {len(report.rows)} 张，一次通过 {sum(1 for r in report.rows if r.outcome == ONE_PASS)} 张，"
        f"返工 {sum(1 for r in report.rows if r.outcome == REWORK)} 张，"
        f"未验收 {sum(1 for r in report.rows if r.outcome == UNREVIEWED)} 张，"
        f"花费 {_cost_cells(total_cost, single_currency)}"
    )
    lines.append("说明：一次通过=只有一次 review 且 verdict 是 accept；返工=被退过（多次 review 或最后一次 reject）；"
                 "一次 review 都没有的单记「未验收」，不算返工。")
    for warning in report.warnings:
        lines.append(f"注意：{warning}")
    return "\n".join(lines)


def main(argv=None) -> int:
    try:  # Windows 控制台默认 GBK，中文表头重定向就变乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(
        description="按工单列出模型、effort、尝试数、验收轮次、花费、token、墙钟，并按模型汇总。只读，不写任何库。"
    )
    parser.add_argument("--db", default=None, help="中心库路径（默认仓库内 .data/task-center.sqlite3）")
    parser.add_argument(
        "--cindy-db", default=None, help="Cindy 主库路径（默认取 %%APPDATA%%/Cindy 下 mtime 最新的 cindy-*.db）"
    )
    args = parser.parse_args(argv)

    try:
        center_path = Path(args.db) if args.db else default_center_db()
        if args.cindy_db:
            cindy_path, candidates = Path(args.cindy_db), []
        else:
            cindy_path, candidates = pick_cindy_db()
        report = build_report(center_path, cindy_path, candidates)
    except ReportError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
