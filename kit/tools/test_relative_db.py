#!/usr/bin/env python
"""只读工具的 `--db` 相对路径：`as_uri()` 不接相对路径
（AW-139 起，AW-141 补 session_roster，AW-142 补 coordinator_state 与 notice_payload）。

`Path.as_uri()` 遇到相对路径抛 `ValueError: relative paths can't be expressed as
file URIs`，于是 `--db .data/task-center.sqlite3` 从一个 traceback 里结束，而不是
一句人话加一个文档化的退出码。`tools/coordinator_state.py` 早就是先
`Path(path).absolute()` 再 `.as_uri()`，这些工具本该同口径。

为什么要一次性对整份清单跑同一条断言：它们各有自己的一份 `read_only()`，
「只改了 next_action」在其余几个上面照样抛 ValueError，而单看 next_action 的用例
一切都是绿的——这正是这张单的反例。清单本身就是覆盖面的定义，不是数字。

清单分两组，因为 `read_only()` 的返回形状不只有一种：

- `TOOLS`：返回一个 `sqlite3.Connection`（五个工具 + coordinator_state）；
- `PAIRED_TOOLS`：返回 `(conn, how)`，`how` 说明走的是 `immutable=1` 还是 `mode=ro`
  （notice_payload）。硬塞进同一组断言只会让 `conn.execute` 在元组上炸掉，
  那不是「相对路径坏了」，会变成一条看不懂的红——所以另写一条。

清单不会自成一体：`test_list_covers_every_read_only_tool` 按源码反查
`tools/*.py`，凡是「自带 `read_only()` 又走 `as_uri()`」的模块都必须落在两份清单里，
新写一个同类工具时漏掉它会当场红。

用例只碰 `read_only()`：它不开 schema 闸门、不读具体表，所以临时库里建一张空表
就够。相对路径靠 `chdir` 进临时目录造（两条路径必须同一个盘符，否则 Windows 上
`os.path.relpath` 直接拒绝），`addCleanup` 保证跑完切回来。

跑法：

    python tools/test_relative_db.py     # 单独跑
    python tools/run_tests.py            # 随主线套件跑
"""
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import close_gate
import coordinator_state
import idle_watch
import invariants
import next_action
import notice_payload
import session_roster
import wait_audit


# 一份清单而不是四段复制：新加一个只读工具时，漏掉它就没被这条断言覆盖。
TOOLS = (
    ("next_action", next_action),
    ("wait_audit", wait_audit),
    ("idle_watch", idle_watch),
    ("close_gate", close_gate),
    ("session_roster", session_roster),
    ("coordinator_state", coordinator_state),
    # AW-159：不变量自检也是只读工具，它最不能容忍的就是「给相对路径就 traceback」——
    # 自检跑不起来的时候，被检查的库里本来就有可疑行。
    ("invariants", invariants),
)

# 返回 `(conn, how)` 的那一类。形状不同，断言也不同：见模块说明。
PAIRED_TOOLS = (
    ("notice_payload", notice_payload),
)


class RelativeDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "task-center.sqlite3"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE probe (x INTEGER)")
        conn.commit()
        conn.close()
        cwd = Path.cwd()
        os.chdir(self.root)  # addCleanup 先注册临时目录、后注册 chdir，回来的顺序是对的
        self.addCleanup(os.chdir, cwd)

    def relative(self, path: Path) -> str:
        """从当前工作目录数出去的那条相对路径，形状与 `--db .data/…` 一样。"""
        return os.path.relpath(path, Path.cwd())

    def test_relative_db_opens_in_all_tools(self):
        rel = self.relative(self.db)
        self.assertFalse(Path(rel).is_absolute(), f"用例本身没造出相对路径：{rel}")
        for name, module in TOOLS:
            with self.subTest(tool=name):
                conn = module.read_only(rel)
                try:
                    self.assertEqual(conn.execute("SELECT count(*) FROM probe").fetchone()[0], 0)
                finally:
                    conn.close()

    def test_relative_db_opens_in_paired_tools(self):
        """返回 `(conn, how)` 的那一类：拆开再断言，不硬塞进上一组。"""
        rel = self.relative(self.db)
        self.assertFalse(Path(rel).is_absolute(), f"用例本身没造出相对路径：{rel}")
        for name, module in PAIRED_TOOLS:
            with self.subTest(tool=name):
                conn, how = module.read_only(rel)
                try:
                    self.assertEqual(conn.execute("SELECT count(*) FROM probe").fetchone()[0], 0)
                finally:
                    conn.close()
                # `how` 是这个工具特意报给人的读取方式，不能随相对路径变成空或异常值。
                self.assertIsInstance(how, str)
                self.assertTrue(
                    "immutable=1" in how or "mode=ro" in how,
                    f"{name}.read_only() 的 how 不是任何一种文档化的读取方式：{how!r}",
                )

    def test_missing_relative_db_reports_absolute_path(self):
        """相对路径也要先落绝对路径：报错里给的是解析后的位置，不是那句相对的。"""
        missing = self.root / "definitely-not-here.sqlite3"
        rel = self.relative(missing)
        for name, module in TOOLS:
            with self.subTest(tool=name):
                with self.assertRaises(Exception) as ctx:
                    module.read_only(rel)
                self.assertIn(str(missing), str(ctx.exception))

    def test_missing_relative_db_reports_absolute_path_in_paired_tools(self):
        missing = self.root / "definitely-not-here.sqlite3"
        rel = self.relative(missing)
        for name, module in PAIRED_TOOLS:
            with self.subTest(tool=name):
                with self.assertRaises(Exception) as ctx:
                    module.read_only(rel)
                self.assertIn(str(missing), str(ctx.exception))

    def test_list_covers_every_read_only_tool(self):
        """清单本身要被源码反查：新写一个同类工具而漏掉它，这条当场红。

        判据是两条同时成立：「自带 `read_only()`」且「走 `Path.as_uri()`」。只满足一条的
        不归这里管：不走 `as_uri()` 的工具开相对路径本来就是好使的；走 `as_uri()` 却没有
        `read_only()` 的（`tools/auto_merge.py` 的 `schema_version_in_database()`）另有它的
        业务断言。
        """
        tools_dir = Path(__file__).resolve().parent
        declared = set()
        for path in sorted(tools_dir.glob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            # 真有一处模块级 `def read_only(` 才算：只凭字符串碰上会把本文件自己也算进去。
            if re.search(r"(?m)^def read_only\s*\(", text) and "as_uri" in text:
                declared.add(path.stem)
        covered = {name for name, _ in TOOLS} | {name for name, _ in PAIRED_TOOLS}
        self.assertEqual(
            declared - covered,
            set(),
            "这些工具自带 read_only() 且走 as_uri()，却没进 TOOLS / PAIRED_TOOLS："
            f"{sorted(declared - covered)}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
