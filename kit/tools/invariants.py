#!/usr/bin/env python
"""不变量自检入口：attempt / revision / artifacts 三类可疑行（AW-159）。

    python tools/invariants.py                              # 对默认库跑一遍
    python tools/invariants.py --project heartopia-script-evolution
    python tools/invariants.py --db .data/task-center.sqlite3 --json
    python tools/invariants.py --list-checks                # 只问「查哪三件事」

三类各查什么、为什么这么查，写在 `task_center/invariants.py` 的模块说明里；这里只
负责把结论打开给人看。**只读**：`mode=ro` + `PRAGMA query_only`，与
`tools/idle_watch.py` / `tools/coordinator_state.py` 同一写法——自检本身绝不能
往被检查的库里写一个字节，否则它就成了它要抓的那种写入。

退出码：0 干净；1 有可疑行（逐条点名，并给出它为什么可疑）；2 库读不了/没初始化。

输出永远是 utf-8（`main()` 一进来就 reconfigure 两个流），不看控制台是什么编码、也
不看 `PYTHONIOENCODING` 设没设：谁读它的输出（测试、管道、别的脚本）都能按 utf-8 解。

为什么它和测试是两件东西：测试证明「工具走的那几条路能拦住」，这个脚本回答「当前
这个库里有没有已经混进去的行」。前者是闸门，后者是体检——HSE-005 的那条
`ae87a2fc` 在工具层没有产出它的路径（`INSERT INTO attempts` 全仓只有 `_claim_ticket`
一处），所以真正抓得到它的是这个体检，不是用例。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from task_center import invariants  # noqa: E402

# 库的默认位置与别的只读工具一致（`tools/work_gate.py` / `tools/next_action.py` 都读这里）。
DEFAULT_DB = ROOT / ".data" / "task-center.sqlite3"


class NotACenter(Exception):
    """库不存在或不是工单中心库——自检无能为力，退出码 2。"""


def read_only(path) -> sqlite3.Connection:
    """只读打开：`mode=ro` + `PRAGMA query_only`，一个字节都不写。

    先落绕到绕对路径再 `as_uri()`：`--db .data/task-center.sqlite3` 这种相对写法是
    文档里的默认用法，而 `as_uri()` 不接相对路径（会抛 ValueError）。自检跑不起来时
    最容易被误读成「库里没问题」。

    这里只负责「打开成只读」——不校验它是不是工单中心库。那不是这一步的职责，
    而且全仓同类只读工具的 `read_only()` 都是这个口径（`tests` 里有一条断言按源码
    反查这份清单）。库不像中心库时，查询会报缺表，由 `main()` 翻成一句人话与退出码 2。
    """
    path = Path(path).absolute()
    if not path.is_file():
        raise NotACenter(f"库不存在：{path}（先 init，或用 --db 指到真正的中心库）")
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        raise NotACenter(f"库打不开：{path}（{exc}）") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def render(report: dict, db: Path, project: str | None) -> str:
    """把结论排成人读的形状：先一句总结，再按三类逐条点名。"""
    scope = f"项目 {project}" if project else "全库"
    lines = [f"不变量自检：{db}（{scope}）"]
    for item in report["checks"]:
        mark = "OK  " if item["suspicious"] == 0 else f"{item['suspicious']:>3} "
        lines.append(f"  {mark}{item['check']}：{item['meaning']}")
    if report["ok"]:
        lines.append("结论：三类全部干净。")
        return "\n".join(lines)
    lines.append(f"结论：{len(report['findings'])} 条可疑行——它们不是工具写进去的（工具每写一次都留一条事件），"
                 "指向的是一次绕过工具的直接写库；逐条看下面，不要自动修。")
    for finding in report["findings"]:
        head = f"  [{finding['check']}] {finding['ticket_id']}"
        if finding.get("attempt_id"):
            head += f" attempt {finding['attempt_id']}"
        if finding.get("submission_id"):
            head += f" submission {finding['submission_id']}"
            if finding.get("artifacts_index") is not None:
                head += f" artifacts[{finding['artifacts_index']}]"
        lines.append(head)
        for key, value in finding.items():
            if key in ("check", "ticket_id", "attempt_id", "submission_id", "artifacts_index", "detail"):
                continue
            lines.append(f"      {key}: {value}")
        lines.append(f"      → {finding['detail']}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    # 输出一律 utf-8，不靠环境变量：Windows 控制台的 `sys.stdout.encoding` 是 cp936，
    # 重定向给别的进程（测试、管道、子进程）时它写出去的就是 GBK 字节，读的人按 utf-8
    # 解就是乱码——AW-159 验收时 `python -m unittest tests.test_invariants` 直接跑就
    # 因此误报 2 条失败，只在 `run_tests.py` 下绿是因为它替子进程设了
    # `PYTHONIOENCODING=utf-8`（`tools/run_tests.py:297`）。本仓库其余只读工具
    # （`close_gate.py` / `idle_watch.py` / `wait_audit.py` …）一进来就 reconfigure，
    # 这里补上同一条；`errors="replace"` 是为了遇到控制台编不出的字符时宁可打问号，
    # 不要因为一次打印失败把所有检查结果一起抛掉。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # stdout 被换成没有 reconfigure 的对象（如某些包装器）
        pass

    parser = argparse.ArgumentParser(
        description="工单库不变量自检：attempt 与 revision 必须在 events 里找得到来源。")
    parser.add_argument("--db", default=str(DEFAULT_DB), help=f"中心库路径（默认 {DEFAULT_DB}）")
    parser.add_argument("--project", default=None, help="只看一个项目（默认全库）")
    parser.add_argument("--json", action="store_true", help="结构化输出，给别的脚本读")
    parser.add_argument("--list-checks", action="store_true", help="只打印三类各查什么")
    args = parser.parse_args(argv)

    if args.list_checks:
        for name in invariants.CHECKS:
            print(f"{name}: {invariants.MEANING[name]}")
        return 0

    try:
        conn = read_only(Path(args.db))
    except NotACenter as exc:
        print(exc, file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"库读不了：{args.db}（{exc}）", file=sys.stderr)
        return 2
    try:
        report = invariants.check(conn, args.project)
    except sqlite3.Error as exc:
        # 库能打开但不是中心库：查询会报缺表。那句话不是人能照着修的，翻一层。
        print(f"{args.db} 不像工单中心库，自检跑不了（{exc}）；这是「没法检」，不是「检完干净」",
              file=sys.stderr)
        return 2
    finally:
        conn.close()

    if args.json:
        print(json.dumps({"db": str(args.db), "project": args.project, **report},
                         ensure_ascii=False, indent=2))
    else:
        print(render(report, Path(args.db), args.project))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
