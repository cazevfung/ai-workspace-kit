"""行尾自检：受管文本在工作树里必须是 LF，属性里必须写着 `eol: lf`。

为什么要有这个脚本：`.gitattributes` 只护着 `skills/**` 的那些日子，同一份文本在
`core.autocrlf=true` 的机器上检出成 CRLF、在别的机器上检出成 LF——内容一样、字节
不同、SHA-256 不同。`task_center/artifacts.py` 记的是**工作树里的字节**，所以换个
人复核，`review_result` 只能报 `ARTIFACT_CHANGED`（AW-137）。

规则写进 `.gitattributes` 只是**声明**，声明与落地是两件事，所以这里两件都核：

1. 声明面：`git check-attr` 里每个受管文本的 `eol` 必须是 `lf`；
2. 落地面：那个文件在工作树里的字节不许含 CRLF。

「受管文本」= 被 git 跟踪、没有被 `.gitattributes` 标成 `-text`、而且不是二进制
（前 8000 字节里没有 NUL，与 git 自己的判定口径一致）。`-text` 与二进制一律按原字节
存取——对它们做换行转换会把文件毁掉（`skills/**` 要与 live 目录逐字节一致，
`.agents/skills/skill-routing/index/**` 是快照，`tests/fixtures/claims/export.bin`
本身就是二进制样板）。

**它核的是「某一次检出」，不是「仓库」。** 所以用例一律在临时克隆里跑
（`tools/test_eol_check.py`）：活工作树可能停在规则合入之前的 CRLF 上，也可能刚被
人改过，拿它当判据会让闸门的临时工作树必红（AW-137 的 C3）。

用法：

    python tools/check_eol.py                    # 核当前检出（默认仓库根）
    python tools/check_eol.py --root <目录>      # 核别的检出，例如临时克隆

退出码：全部成立 0，有违反 1，用法错误或不是 git 检出 2。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 与 git 的二进制判定同口径：只看前 8000 字节里有没有 NUL。
BINARY_PROBE = 8000


def has_crlf(data: bytes) -> bool:
    """工作树字节里有没有 CRLF。单独一个 CR 不算：git 也只把 CRLF 当行尾。"""
    return b"\r\n" in data


def is_binary(data: bytes) -> bool:
    """是不是二进制（前 8000 字节里有 NUL）。"""
    return b"\x00" in data[:BINARY_PROBE]


@dataclass
class Report:
    """一次扫描的结论。problems 是给人看的、每条都点名路径。"""

    problems: list[str] = field(default_factory=list)
    managed: list[str] = field(default_factory=list)
    skipped: int = 0

    def ok(self) -> bool:
        return not self.problems


def _git(root: Path, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        input=stdin, capture_output=True, timeout=600,
    )


def _decode(raw: bytes) -> str:
    # 路径里可能有非 UTF-8 字节；用 surrogateescape 原样带回，好让报错能定位到它。
    return raw.decode("utf-8", "surrogateescape")


def tracked(root: Path) -> list[str]:
    """被 git 跟踪的全部路径（`-z` 读，中文与空格路径不会被转义）。"""
    out = _git(root, "ls-files", "-z").stdout
    return [_decode(item) for item in out.split(b"\x00") if item]


def attributes(root: Path, paths: list[str]) -> dict[str, dict[str, str]]:
    """批量读 `text` 与 `eol` 两个属性，返回 {路径: {属性: 取值}}。

    一次 `check-attr --stdin -z` 读完全部路径：逐个文件起一个进程在 300 个文件上
    要几十秒，而这条自检在每次合并里要跑两遍。
    """
    if not paths:
        return {}
    payload = b"\x00".join(path.encode("utf-8", "surrogateescape") for path in paths)
    payload += b"\x00"
    out = _git(root, "check-attr", "-z", "--stdin", "text", "eol", stdin=payload).stdout
    parts = out.split(b"\x00")
    table: dict[str, dict[str, str]] = {}
    # `-z` 的形式是「路径 \0 属性名 \0 取值 \0」三段一组，路径里的特殊字符不再转义。
    for index in range(0, len(parts) - 2, 3):
        path = _decode(parts[index])
        name = _decode(parts[index + 1])
        value = _decode(parts[index + 2])
        table.setdefault(path, {})[name] = value
    return table


def scan(root: Path) -> Report:
    """扫一次检出，返回结论。root 必须是 git 检出的根（或其中任意位置亦可）。"""
    root = Path(root)
    report = Report()
    paths = tracked(root)
    table = attributes(root, paths)
    for path in paths:
        attr = table.get(path, {})
        target = root / path
        try:
            data = target.read_bytes()
        except OSError:
            report.problems.append(f"{path}: 读不到工作树里的文件（它被删掉了，或不是一个普通文件）")
            continue
        if attr.get("text") == "unset" or is_binary(data):
            report.skipped += 1
            continue
        report.managed.append(path)
        eol = attr.get("eol", "unspecified")
        if eol != "lf":
            report.problems.append(
                f"{path}: eol 属性是 {eol}，应为 lf（.gitattributes 没把这条路径钉住）"
            )
        if has_crlf(data):
            report.problems.append(
                f"{path}: 工作树里是 CRLF，应为 LF（这条检出没有按规则重检出）"
            )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="行尾自检：受管文本必须是 LF")
    parser.add_argument("--root", default=str(ROOT), help="要核的检出目录（默认本仓库根）")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"用法错误：{root} 不是一个目录", file=sys.stderr)
        return 2
    probe = _git(root, "rev-parse", "--show-toplevel")
    if probe.returncode != 0:
        print(f"用法错误：{root} 不是一个 git 检出\n{probe.stderr.decode('utf-8', 'replace')}",
              file=sys.stderr)
        return 2
    report = scan(root)
    if report.ok():
        print(f"行尾自检通过：{len(report.managed)} 个受管文本全是 LF"
              f"（跳过 {report.skipped} 个 -text / 二进制）")
        return 0
    print(f"行尾自检失败：{len(report.problems)} 处违反（受管文本 {len(report.managed)} 个）")
    for problem in report.problems:
        print(f"  - {problem}")
    print("修法：确认 .gitattributes 里有 `* text=auto eol=lf`，再按 README 的「行尾口径」"
          "就地重检出（会丢未提交改动）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
