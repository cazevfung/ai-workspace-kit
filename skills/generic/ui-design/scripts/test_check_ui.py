#!/usr/bin/env python3
"""`check_ui.py` 自己的反例用例：每条规则先造一次红，再改对一次变绿。

为什么要有这个文件：一个永远绿的检查器等于没有检查器。所以九条规则各写一个最小样本，
红的那份必须让脚本退出 1 并点名那条规则，绿的那份必须退出 0。另外补几项结构性事实：
干净文件 0 问题、目录递归、`fixtures/` 默认跳过、注释与 `<svg>` 与 `data:` URI 不算、
真实的 `tokens.css` 与 `page-template.html` 必须 0 问题、反例页必须真的红。

输出每个用例一行 `PASS <名字>`；有失败就是 `FAIL <名字>: 原因`，最后统计，全过退出 0。

用法：

    python skills/ui-design/scripts/test_check_ui.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
CHECK = SCRIPTS / "check_ui.py"
ROOT = SCRIPTS.parent
FIXTURE = SCRIPTS / "fixtures" / "bad-page.html"
TOKENS = ROOT / "assets" / "tokens.css"
TEMPLATE = ROOT / "assets" / "page-template.html"

# 每条规则一份最小红样本、一份改对后的绿样本。样本刻意写得小，
# 出问题时一眼能看出是哪一行触发的。
RULE_CASES: dict[str, tuple[dict[str, str], dict[str, str]]] = {
    "color": (
        {"bad.css": "body { color: #0F0F0F; }\n"},
        {"good.css": ":root { --color-text-primary: #0F0F0F; }\nbody { color: var(--color-text-primary); }\n"},
    ),
    "space": (
        {"bad.css": ".a { padding: 10px; }\n"},
        {"good.css": ".a { padding: 12px; }\n"},
    ),
    "radius": (
        {"bad.css": ".a { border-radius: 12px; }\n"},
        {"good.css": ".a { border-radius: 16px; }\n"},
    ),
    "type": (
        {"bad.css": ".a { font-size: 14px; }\n"},
        {"good.css": ".a { font-size: 15px; }\n"},
    ),
    "motion": (
        {"bad.css": ".a { transition: color 200ms ease; }\n@keyframes k { from { opacity: 0; } to { opacity: 1; } }\n"
                    "@media (prefers-reduced-motion: reduce) { .a { transition: none; } }\n"},
        {"good.css": ".a { transition: color 250ms ease; }\n"
                     "@media (prefers-reduced-motion: reduce) { .a { transition: none; } }\n"},
    ),
    "reduced-motion": (
        {"bad.css": ".a { transition: color 250ms ease; }\n"},
        {"good.css": ".a { transition: color 250ms ease; }\n"
                     "@media (prefers-reduced-motion: reduce) { .a { transition: none; } }\n"},
    ),
    "glyph": (
        {"bad.html": "<button>打开菜单 ›</button>\n"},
        {"good.html": "<button>打开菜单</button>\n"},
    ),
    "focus": (
        {"bad.css": "button { outline: none; }\n"},
        {"good.css": "button { outline: none; }\nbutton:focus-visible { outline: 2px solid var(--color-link); }\n"},
    ),
    "icon-mix": (
        {"bad.html": "<link rel=\"stylesheet\" href=\"material-icons.css\">\n"},
        {"good.html": "<link rel=\"stylesheet\" href=\"app.css\">\n"},
    ),
}


def _utf8_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def run(targets: list[Path]) -> tuple[int, str]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, str(CHECK)] + [str(target) for target in targets],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def write(directory: Path, files: dict[str, str]) -> list[Path]:
    written = []
    for name, content in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passes = 0

    def ok(self, name: str) -> None:
        self.passes += 1
        print("PASS %s" % name)

    def fail(self, name: str, reason: str) -> None:
        self.failures.append(name)
        print("FAIL %s: %s" % (name, reason))

    def check(self, name: str, condition: bool, reason: str) -> None:
        if condition:
            self.ok(name)
        else:
            self.fail(name, reason)


def test_rule(report: Report, rule: str) -> None:
    """一条规则一次判两件事：红样本被点名，绿样本退出 0。"""
    bad, good = RULE_CASES[rule]
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        bad_paths = write(directory, bad)
        code, output = run(bad_paths)
        if code != 1:
            report.fail(rule, "红样本退出 %d，期望 1；输出 %r" % (code, output.strip()[:200]))
            return
        if "[%s]" % rule not in output:
            report.fail(rule, "红样本没有指出 [%s]；输出 %r" % (rule, output.strip()[:200]))
            return

        for path in bad_paths:
            path.unlink()
        good_paths = write(directory, good)
        code, output = run(good_paths)
        if code != 0:
            report.fail(rule, "绿样本退出 %d，期望 0；输出 %r" % (code, output.strip()[:300]))
            return
    report.ok(rule)


def test_clean(report: Report) -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        paths = write(directory, {
            "clean.css": ":root { --space-4: 16px; }\n"
                         ".a { padding: var(--space-4); border-radius: 16px; font-size: 16px; }\n"
                         ".a { transition: transform 120ms ease; }\n"
                         "@media (prefers-reduced-motion: reduce) { .a { transition: none; } }\n",
        })
        code, output = run(paths)
    report.check("clean", code == 0 and "0 处问题" in output,
                 "干净文件应退出 0 并报 0 处问题；实际退出 %d，输出 %r" % (code, output.strip()[:200]))


def test_dir_recursive(report: Report) -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        write(directory, {"pages/deep/nested/bad.css": ".a { padding: 10px; }\n"})
        code, output = run([directory])
    report.check("dir-recursive", code == 1 and "nested" in output,
                 "目录递归应扫到子目录里的红样本；实际退出 %d，输出 %r" % (code, output.strip()[:200]))


def test_fixtures_skipped(report: Report) -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        write(directory, {
            "ok.css": ".a { padding: 16px; }\n",
            "fixtures/bad.css": ".a { padding: 10px; }\n",
        })
        code, output = run([directory])
    report.check("fixtures-skipped", code == 0,
                 "目录里的 fixtures/ 应默认跳过；实际退出 %d，输出 %r" % (code, output.strip()[:200]))


def test_fixture_bad_page(report: Report) -> None:
    code, output = run([FIXTURE])
    count = len(re.findall(r"\[\w[\w-]*\]", output))
    rules = {rule for rule in RULE_CASES if "[%s]" % rule in output}
    report.check("bad-page", code == 1 and count >= 8 and len(rules) == len(RULE_CASES),
                 "反例页应退出 1、至少 8 条问题、九条规则都出现；实际退出 %d、%d 条、命中规则 %d 条"
                 % (code, count, len(rules)))


def test_tokens_css(report: Report) -> None:
    code, output = run([TOKENS])
    report.check("tokens-css", code == 0,
                 "tokens.css 是色值唯一来源，必须 0 问题；实际退出 %d，输出 %r" % (code, output.strip()[:200]))


def test_template(report: Report) -> None:
    code, output = run([TEMPLATE])
    report.check("page-template", code == 0,
                 "单文件模板必须 0 问题；实际退出 %d，输出 %r" % (code, output.strip()[:300]))


def test_svg_skipped(report: Report) -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        paths = write(directory, {
            "icon.html": "<svg viewBox=\"0 0 24 24\"><path d=\"M0 0h24v24H0z\" fill=\"#FF0000\"/></svg>\n",
        })
        code, output = run(paths)
    report.check("svg-skipped", code == 0,
                 "<svg> 里的图形不该算界面代码；实际退出 %d，输出 %r" % (code, output.strip()[:200]))


def test_data_uri_skipped(report: Report) -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        paths = write(directory, {
            "bg.css": ".a { background: url(data:image/svg+xml;base64,AAAA#FFFFFF); }\n",
        })
        code, output = run(paths)
    report.check("data-uri-skipped", code == 0,
                 "data: URI 里的内容不该算色值；实际退出 %d，输出 %r" % (code, output.strip()[:200]))


def test_comment_skipped(report: Report) -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        paths = write(directory, {
            "note.css": "/* 旧写法：padding: 10px; color: #FFFFFF; */\n.a { padding: 16px; }\n",
        })
        code, output = run(paths)
    report.check("comment-skipped", code == 0,
                 "注释里的旧写法不该算问题；实际退出 %d，输出 %r" % (code, output.strip()[:200]))


def test_missing_path(report: Report) -> None:
    code, output = run([Path("no-such-file.css")])
    report.check("usage-error", code == 2,
                 "路径不存在应以 2 退出（用法错误，不是「有问题」）；实际退出 %d，输出 %r"
                 % (code, output.strip()[:200]))


def main() -> int:
    _utf8_streams()
    if not CHECK.is_file():
        print("FAIL setup: 找不到 %s" % CHECK)
        return 1
    report = Report()
    for rule in RULE_CASES:
        test_rule(report, rule)
    test_clean(report)
    test_dir_recursive(report)
    test_fixtures_skipped(report)
    test_fixture_bad_page(report)
    test_tokens_css(report)
    test_template(report)
    test_svg_skipped(report)
    test_data_uri_skipped(report)
    test_comment_skipped(report)
    test_missing_path(report)

    total = report.passes + len(report.failures)
    if report.failures:
        print("共 %d 项，%d 项未通过：%s" % (total, len(report.failures), "、".join(report.failures)))
        return 1
    print("共 %d 项全部通过" % total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
