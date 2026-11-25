#!/usr/bin/env python3
"""把「界面跑偏了」变成退出码。

输入是文件或目录（目录递归 `.html` / `.css` / `.tsx` / `.jsx` / `.vue`），
输出每处问题一行 `文件:行号 [规则名] 说明`，末尾一行汇总。
有问题退出 1，没问题退出 0，参数不对退出 2。

九条规则（名字固定，脚本输出与文档表格用同一批名字）：

  color           裸色值：`#RRGGBB`、`rgb(`、`hsl(` 出现在 `tokens.css` 之外或 `:root` 之外
  space           padding / margin / gap 的数值必须是 0/2/4/8/12/16/24/32
  radius          圆角必须是 0/4/16/20/24/9999/50%
  type            font-size 必须落在 11/12/13/15/16/17/18/20/24
  motion          时长必须是 120/150/250/320/900/1100ms；出现 `transition: all` 直接报
  reduced-motion  文件里有 animation / transition，却没有 prefers-reduced-motion 媒体查询
  glyph           把 › ‹ ⋯ ☰ ✓ ✕ ★ → 当图标用；emoji 也算
  focus           `outline: none` / `outline: 0` 而没有 `:focus-visible` 替代
  icon-mix        出现 font-awesome / material-icons / iconfont / bootstrap-icons 等第二套图标库

三处默认：`<svg>` 内容、注释、`data:` URI 一律不算（它们不是给人看的界面代码）；
目录里名为 `fixtures` 的子目录默认跳过（那些样本本来就是用来红的，要查就把文件当参数点名）；
`tokens.css` 里的色值全部放行，它就是唯一允许写十六进制的地方。

用法：

    python skills/ui-design/scripts/check_ui.py skills/ui-design
    python skills/ui-design/scripts/check_ui.py assets/page-template.html
    python skills/ui-design/scripts/check_ui.py scripts/fixtures/bad-page.html   # 期望退出 1
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

RULES = (
    "color",
    "space",
    "radius",
    "type",
    "motion",
    "reduced-motion",
    "glyph",
    "focus",
    "icon-mix",
)

SCAN_SUFFIXES = (".html", ".htm", ".css", ".tsx", ".jsx", ".vue")
SKIP_DIRS = {"fixtures", "node_modules", ".git", "__pycache__", ".venv", "dist", "build"}
TOKEN_FILE = "tokens.css"

SPACE_ALLOWED = {0, 2, 4, 8, 12, 16, 24, 32}
RADIUS_ALLOWED = {0, 4, 16, 20, 24, 9999}
TYPE_ALLOWED = {11, 12, 13, 15, 16, 17, 18, 20, 24}
DURATION_ALLOWED = {0, 120, 150, 250, 320, 900, 1100}

COLOR_TOKEN = re.compile(r"#[0-9A-Fa-f]{8}\b|#[0-9A-Fa-f]{6}\b|#[0-9A-F]{3}\b|(?<![-\w])(?:rgba?|hsla?)\(")
SPACE_DECL = re.compile(
    r"(?:^|[;{\s])(?:padding|margin|gap|row-gap|column-gap)"
    r"(?:-(?:top|right|bottom|left|inline|inline-start|inline-end|block|block-start|block-end))?"
    r"\s*:\s*([^;}]+)"
)
RADIUS_DECL = re.compile(
    r"border(?:-(?:top|right|bottom|left))?(?:-(?:start|end))?(?:-(?:start|end))?-radius\s*:\s*([^;}]+)"
)
FONT_SIZE_DECL = re.compile(r"font-size\s*:\s*([^;}]+)")
MOTION_DECL = re.compile(
    r"(?:^|[;{\s])(?:transition|animation|--duration)(?:-[a-z-]+)?\s*:\s*([^;}]+)"
)
TRANSITION_ALL = re.compile(r"(?:transition|transition-property)\s*:\s*all\b")
DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)(ms|s)\b")
HAS_MOTION = re.compile(r"(?:^|[;{\s])(?:animation|transition)(?:-[a-z-]+)?\s*:|@keyframes\b")
OUTLINE_RESET = re.compile(r"outline\s*:\s*(?:none|0)\b")
GLYPH_CHARS = "›‹⋯☰✓✕★→←↑↓"
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]")
ICON_LIBS = re.compile(
    r"font-?awesome|@fortawesome|material-icons|material-symbols|iconfont|bootstrap-icons|ionicons",
    re.IGNORECASE,
)
VAR_CALL = re.compile(r"var\([^()]*(?:\([^()]*\)[^()]*)*\)")
NUMBER = re.compile(r"(-?\d+(?:\.\d+)?)\s*(px|rem|em|%|pt)?")


class Problem:
    def __init__(self, path: Path, line: int, rule: str, message: str) -> None:
        self.path = path
        self.line = line
        self.rule = rule
        self.message = message

    def render(self) -> str:
        return "%s:%d [%s] %s" % (self.path, self.line, self.rule, self.message)


def _utf8_streams() -> None:
    """Windows 控制台默认按本地代码页编码，中文输出会变乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def blank(text: str, pattern: str) -> str:
    """把命中的片段换成等量换行，行号不变、内容不再被后面几条规则看到。"""
    def replace(match: re.Match) -> str:
        return "\n" * match.group(0).count("\n")
    return re.sub(pattern, replace, text, flags=re.DOTALL)


def mask(text: str) -> str:
    """抹掉注释、`<svg>` 内嵌图形、`data:` URI：它们不是给人看的界面代码。"""
    text = blank(text, r"<!--.*?-->")
    text = blank(text, r"/\*.*?\*/")
    text = blank(text, r"<svg\b.*?</svg>")
    text = blank(text, r"data:[^\"')\s]+")
    return text


def root_flags(text: str) -> dict[int, bool]:
    """标出「这一行处在选择器含 :root 的块里」，色值只在那里放行。"""
    flags: dict[int, bool] = {}
    stack: list[bool] = []
    buf: list[str] = []
    line = 1
    inside = False
    for ch in text:
        if ch == "\n":
            line += 1
        elif ch == "{":
            stack.append(":root" in "".join(buf))
            inside = any(stack)
            buf = []
        elif ch == "}":
            if stack:
                stack.pop()
            inside = any(stack)
            buf = []
        elif ch == ";":
            buf = []
        else:
            buf.append(ch)
        if inside:
            flags[line] = True
    return flags


def strip_var(value: str) -> str:
    """去掉 `var(...)` 调用，只留下真正写死的数值。"""
    previous = None
    while previous != value:
        previous = value
        value = VAR_CALL.sub(" ", value)
    return value


def numbers_of(value: str) -> list[tuple[float, str]]:
    found = []
    for raw, unit in NUMBER.findall(strip_var(value)):
        # findall 会把「没参与匹配的分组」给成空串，归一回 None，后面几处判断只认 None。
        found.append((float(raw), unit or None))
    return found


def check_color(path: Path, lines: list[str], roots: dict[int, bool]) -> list[Problem]:
    if path.name == TOKEN_FILE:
        return []
    problems = []
    for number, text in enumerate(lines, 1):
        if roots.get(number):
            continue
        for match in COLOR_TOKEN.finditer(text):
            problems.append(Problem(path, number, "color",
                                    "裸色值 %s：页面只写 var(--color-*)，色值只留在 tokens.css" % match.group(0)))
            break
    return problems


def check_space(path: Path, lines: list[str], roots: dict[int, bool]) -> list[Problem]:
    problems = []
    for number, text in enumerate(lines, 1):
        for match in SPACE_DECL.finditer(text):
            for value, unit in numbers_of(match.group(1)):
                if unit in (None, "px") and abs(value) in SPACE_ALLOWED:
                    continue
                problems.append(Problem(
                    path, number, "space",
                    "间距 %s%s 不在档上：只用 0/2/4/8/12/16/24/32，或 var(--space-*)"
                    % (("%g" % value), unit or "")))
                break
    return problems


def check_radius(path: Path, lines: list[str], roots: dict[int, bool]) -> list[Problem]:
    problems = []
    for number, text in enumerate(lines, 1):
        for match in RADIUS_DECL.finditer(text):
            for value, unit in numbers_of(match.group(1)):
                if unit == "%" and value == 50:
                    continue
                if unit in (None, "px") and abs(value) in RADIUS_ALLOWED:
                    continue
                problems.append(Problem(
                    path, number, "radius",
                    "圆角 %s%s 不在档上：只用 0/4/16/20/24/9999 与 50%%，或 var(--radius-*)" % (("%g" % value), unit or "")))
                break
    return problems


def check_type(path: Path, lines: list[str], roots: dict[int, bool]) -> list[Problem]:
    problems = []
    for number, text in enumerate(lines, 1):
        for match in FONT_SIZE_DECL.finditer(text):
            for value, unit in numbers_of(match.group(1)):
                if unit in (None, "px") and value in TYPE_ALLOWED:
                    continue
                problems.append(Problem(
                    path, number, "type",
                    "字号 %s%s 不在阶梯上：只用 11/12/13/15/16/17/18/20/24，或 var(--text-*)" % (("%g" % value), unit or "")))
                break
    return problems


def check_motion(path: Path, lines: list[str], roots: dict[int, bool]) -> list[Problem]:
    problems = []
    for number, text in enumerate(lines, 1):
        if TRANSITION_ALL.search(text):
            problems.append(Problem(path, number, "motion",
                                    "transition: all 会把颜色、尺寸、布局一起补间，改成具体属性"))
            continue
        for match in MOTION_DECL.finditer(text):
            for raw, unit in DURATION_TOKEN.findall(match.group(1)):
                milliseconds = float(raw) * (1000 if unit == "s" else 1)
                if milliseconds in DURATION_ALLOWED:
                    continue
                problems.append(Problem(
                    path, number, "motion",
                    "时长 %s%s 不在档上：只用 120/150/250/320/900/1100ms，或 var(--duration-*)" % (raw, unit)))
                break
    return problems


def check_reduced_motion(path: Path, lines: list[str], roots: dict[int, bool], text: str) -> list[Problem]:
    if "prefers-reduced-motion" in text:
        return []
    for number, line in enumerate(lines, 1):
        if HAS_MOTION.search(line):
            return [Problem(path, number, "reduced-motion",
                            "有 animation / transition 却没有 prefers-reduced-motion 媒体查询")]
    return []


def check_glyph(path: Path, lines: list[str], roots: dict[int, bool]) -> list[Problem]:
    problems = []
    for number, text in enumerate(lines, 1):
        hit = next((char for char in GLYPH_CHARS if char in text), None)
        if hit is not None:
            problems.append(Problem(path, number, "glyph",
                                    "字符图标「%s」当图标用了：换成 Lucide SVG" % hit))
            continue
        emoji = EMOJI.search(text)
        if emoji is not None:
            problems.append(Problem(path, number, "glyph",
                                    "emoji「%s」当图标用了：换成 Lucide SVG" % emoji.group(0)))
    return problems


def check_focus(path: Path, lines: list[str], roots: dict[int, bool], text: str) -> list[Problem]:
    if ":focus-visible" in text:
        return []
    for number, line in enumerate(lines, 1):
        if OUTLINE_RESET.search(line):
            return [Problem(path, number, "focus",
                            "outline 被去掉却没有 :focus-visible 替代，键盘用户看不到焦点")]
    return []


def check_icon_mix(path: Path, lines: list[str], roots: dict[int, bool]) -> list[Problem]:
    for number, line in enumerate(lines, 1):
        match = ICON_LIBS.search(line)
        if match:
            return [Problem(path, number, "icon-mix",
                            "混进第二套图标库 %s：图标只从 Lucide 取" % match.group(0))]
    return []


def check_file(path: Path) -> list[Problem]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    text = mask(raw)
    lines = text.splitlines()
    roots = root_flags(text)
    problems: list[Problem] = []
    problems += check_color(path, lines, roots)
    problems += check_space(path, lines, roots)
    problems += check_radius(path, lines, roots)
    problems += check_type(path, lines, roots)
    problems += check_motion(path, lines, roots)
    problems += check_reduced_motion(path, lines, roots, text)
    problems += check_glyph(path, lines, roots)
    problems += check_focus(path, lines, roots, text)
    problems += check_icon_mix(path, lines, roots)
    problems.sort(key=lambda problem: (problem.line, RULES.index(problem.rule)))
    return problems


def collect(targets: list[str]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        path = Path(target)
        if path.is_file():
            files.append(path)
            continue
        if not path.is_dir():
            print("用法错误：%s 既不是文件也不是目录" % target)
            raise SystemExit(2)
        for child in sorted(path.rglob("*")):
            if not child.is_file() or child.suffix.lower() not in SCAN_SUFFIXES:
                continue
            if any(part in SKIP_DIRS for part in child.relative_to(path).parts[:-1]):
                continue
            files.append(child)
    return files


def main(argv: list[str] | None = None) -> int:
    _utf8_streams()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__.strip())
        return 2
    files = collect(args)
    problems: list[Problem] = []
    for path in files:
        problems += check_file(path)
    for problem in problems:
        print(problem.render())
    if problems:
        print("共 %d 个文件，%d 处问题（规则：%s）" % (len(files), len(problems), "/".join(RULES)))
        return 1
    print("共 %d 个文件，0 处问题" % len(files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
