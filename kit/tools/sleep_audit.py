#!/usr/bin/env python
"""轮内 sleep 等下游：扫会话日志里每一条 shell 命令，把「睡完干什么」按时间倒序列出来。

为什么要有这个脚本：`AGENTS.md` 六-b、`docs/protocol.md`、`ticket-dispatch`
三处都写着「dispatch 完就结束本轮，禁止轮内 sleep 等下游」，但三处都只是文字。
`tools/wait_audit.py` 报的是「谁在等谁」的四类症状（等验收超 4 小时没 review 行、
`done` 没回执、`done` 没合并、`running` 但执行会话 10 分钟没活动），而轮内 sleep
是 2–30 分钟一轮「睡完再查一次库」，四类一条都不触发——2026-11-24 实测一条协调
会话在 CMO-004 上轮内 sleep 19 次（120–300 秒），系统提示里就带着那条禁止原文。
所以这条规则缺一个确定性的检出项，这个脚本就是那个面：它读会话日志，把每一条
命令里的 sleep 找出来，连同「睡完之后干了什么」一起打印。

**命中判据（写死在这里，别在用例与文档里各写一份）：**

1. 只看工具调用里的 shell 命令，即 `message.content` 里的
   `{"type":"toolCall","arguments":{"command":"..."}}` 块（`bash` / `powershell`
   都用 `command` 这个键）。工具结果块 `toolResult` 不看：那里面是被回显的文本，
   不是谁执行的命令。
2. `sleep` 必须处在**命令位置**：位于一段命令的开头，或者紧跟在 `;` `&&` `||`
   `|` `&` 换行之后（允许前面只有 `VAR=...` 赋值与 `time` / `nohup` / `sudo` /
   `env` / `command` / `exec` / `timeout N` 这类包装）。这样 `grep -l "sleep 270"
   *.jsonl`、`echo sleep 300`、python 里拼一段含 `'sleep 27...'` 的文本都不算命中——
   2026-11-24 手扫时正是这四类字符串误命中过 4 次。
3. 秒数写在那条命令的第一个参数上（`sleep 270`、`sleep 30.5`、`sleep 1m`），
   或 PowerShell 的 `Start-Sleep -Seconds 270` / `Start-Sleep 270`；**换算后 ≥ 30 秒**
   才算命中。30 秒以下不报：等 UI 渲染、等一次写盘的那种短 sleep 是正常的，把它也
   报出来，清单里就全是噪声，人会学会跳过这份清单。
4. 同一条命令里的多个 sleep 各算一条（`sleep 60; sleep 90` 是两次等待）。
5. 窗口按工具调用所在记录的 `timestamp` 算，默认最近 24 小时（`--hours`）；
   `--hours 0` 表示不限窗口。

**输出三块**，都按时间倒序或按命中数排：

- 命中：一行一条 `时间  秒数  会话文件名  睡完之后的部分`；
- 会话一览：本窗口内出现过 sleep 命令的会话（含未达阈值的），带「命中 / sleep 命令」
  两个计数——用来回答「这条会话到底有没有在轮内等过」，未达阈值的那部分不算命中。
  这一块只在有命中时打印：没有命中就只留结论，免得把 `sleep 5` 读成命中；
- 末行合计：扫了多少会话、多少有 sleep 命令、其中多少有命中、命中几条。

只读：不写库、不改日志。缺席与「没有问题」必须分开，所以目录不存在是退出码 2
（`SESSIONS_DIR_NOT_FOUND`），不是 0。

    python tools/sleep_audit.py                          # 最近 24 小时，人读
    python tools/sleep_audit.py --hours 72               # 换窗口
    python tools/sleep_audit.py --hours 0                # 不限窗口
    python tools/sleep_audit.py --json                   # 结构化结果
    python tools/sleep_audit.py --sessions-dir <目录>     # 换日志目录（用例用）

退出码：0 = 没有命中；1 = 有命中；2 = 参数或目录有问题（原因写 stderr）。
它检出什么就是违规，不是提示：这份清单里每一条都是「这一轮押在了回执一定会到上」。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# 阈值与窗口只在常量里写一次，旁边标出处；用例与文档都打到这里，不各写一份。
# 30 秒：短 sleep 是等 UI 渲染（2026-11-24 的记录里有大量 1–8 秒的），
# 而这一次要抓的轮内等待是 120–1800 秒；出处见工单 AW-145 与
# `.memory` 里的 `project_in-round-sleep-not-detected`。
MIN_SLEEP_SECONDS = 30.0
# 24 小时：默认只看最近一天，读整仓 1500+ 份日志太慢，且旧事故有单独记录。
DEFAULT_HOURS = 24.0

# `sleep` 前面允许出现、不属于「命令名」的那些词：包装器与赋值不算改变命令位置。
WRAPPERS = {"time", "nohup", "sudo", "env", "command", "exec", "builtin"}
# 这些包装器后面跟的是「跑什么」，不是自己的选项，遇到就继续往右找命令名。
VALUE_WRAPPERS = {"timeout", "nice", "ionice", "stdbuf", "ssh"}
SHELLS = {"bash", "sh", "zsh", "dash", "pwsh", "powershell", "cmd", "cmd.exe"}

# `sleep` 的参数：整数/小数，可带 s / m / h / d 单位（GNU sleep 的写法）。
DURATION = re.compile(r"^(\d+(?:\.\d+)?)([smhd]?)$", re.IGNORECASE)
UNIT_SECONDS = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SESSION_ID = re.compile(r"_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
                        re.IGNORECASE)

# 打印「睡完之后的部分」时截多长：够看清下一步，不至于把整段 heredoc 糊上来。
TAIL_CHARS = 160
# JSON 里附带的原始命令也截一刀：命令可能带 heredoc，几十 KB 一条不适合当证据行。
COMMAND_CHARS = 600


class AuditError(RuntimeError):
    """查不出结论时的唯一异常：带一句人能读的原因，由 main 打到 stderr。"""


# ---------------------------------------------------------------- 命令解析


@dataclass
class Token:
    """一个词。`value` 去掉引号，`start` / `end` 是它在原字符串里的位置。"""

    value: str
    start: int
    end: int
    quoted: bool = False


def split_commands(text: str) -> list[list[Token]]:
    """按未加引号的分隔符（`;` `&&` `||` `|` `&` 换行）切成段，每段切成词。

    自己写而不是 `shlex.split` 的全部理由：要拿位置算「sleep 之后的部分」，
    而 shlex 只给值不给偏移；另外要区分「词在引号里」与不在，避免把
    `grep "sleep 270"` 里的字符串当成命令。
    """
    segments: list[list[Token]] = []
    tokens: list[Token] = []
    buf: list[str] = []
    start = -1
    end_pos = -1
    index = 0
    length = len(text)

    def begin() -> None:
        nonlocal start, end_pos
        if not buf:
            start = index
            end_pos = index

    def flush(quoted: bool = False) -> None:
        nonlocal buf, start, end_pos
        if buf:
            tokens.append(Token("".join(buf), start, end_pos, quoted))
            buf = []
            start = -1
            end_pos = -1

    def close_segment() -> None:
        nonlocal tokens
        flush()
        if tokens:
            segments.append(tokens)
        tokens = []

    while index < length:
        char = text[index]
        if char in "'\"":
            quote = char
            begin()
            index += 1
            end_pos = index
            while index < length:
                char = text[index]
                if char == "\\" and quote == '"' and index + 1 < length:
                    buf.append(text[index + 1])
                    index += 2
                    end_pos = index
                    continue
                if char == quote:
                    index += 1
                    end_pos = index
                    break
                buf.append(char)
                index += 1
                end_pos = index
            flush(quoted=True)
            continue
        if char == "\\" and index + 1 < length:
            begin()
            buf.append(text[index + 1])
            index += 2
            end_pos = index
            continue
        if text.startswith("&&", index) or text.startswith("||", index):
            close_segment()
            index += 2
            continue
        if char in ";|\n&":
            close_segment()
            index += 1
            continue
        if char.isspace():
            flush()
            index += 1
            continue
        begin()
        buf.append(char)
        index += 1
        end_pos = index
    close_segment()
    return segments


def is_sleep_word(value: str) -> bool:
    """`sleep` / `/bin/sleep` / `Start-Sleep` 都认；`sleep(270)` 这类不算。"""
    lowered = value.lower()
    if lowered in ("sleep", "start-sleep"):
        return True
    return lowered.endswith("/sleep") or lowered.endswith("\\sleep")


def parse_duration(value: str) -> float | None:
    matched = DURATION.match(value.strip().strip("'\""))
    if not matched:
        return None
    return float(matched.group(1)) * UNIT_SECONDS[matched.group(2).lower()]


def command_position(tokens: list[Token], index: int) -> bool:
    """下标 `index` 处的词是不是这条子命令的命令名。

    允许前面只有赋值（`FOO=1 sleep 30`）与包装器（`timeout 60 sleep 30`）；
    包装器自己的选项（`sudo -u root`）一并跳过。`grep -l "sleep 270"` 里
    `sleep` 前面是 `grep`，不是命令位置，所以不算。
    """
    cursor = 0
    while cursor < index:
        value = tokens[cursor].value
        if ASSIGNMENT.match(value):
            cursor += 1
            continue
        if value in WRAPPERS:
            cursor += 1
            while cursor < index and tokens[cursor].value.startswith("-"):
                cursor += 1
            continue
        if value in VALUE_WRAPPERS:
            cursor += 1
            while cursor < index and (tokens[cursor].value.startswith("-")
                                      or DURATION.match(tokens[cursor].value)):
                cursor += 1
            continue
        return False
    return True


def tail_after(text: str, offset: int) -> str:
    """命令里 sleep 之后的部分：去掉前导分隔符、压平空白、截断。"""
    tail = re.sub(r"\s+", " ", text[offset:]).strip()
    tail = re.sub(r"^(?:[;|&]\s*)+", "", tail)
    if len(tail) > TAIL_CHARS:
        tail = tail[:TAIL_CHARS].rstrip() + "…"
    return tail


def sleep_calls(text: str, _depth: int = 0) -> list[dict]:
    """一条命令里的全部 sleep：`[{"seconds": 270.0, "tail": "cd ..."}]`。

    `bash -c "...; sleep 300"` / `powershell -Command "Start-Sleep -Seconds 300"`
    会递归进那层引号内容里找——那种写法在日志里存在，不递进去就会漏。
    """
    found: list[dict] = []
    for tokens in split_commands(text):
        index = 0
        while index < len(tokens):
            token = tokens[index]
            value = token.value
            if ASSIGNMENT.match(value) or value in WRAPPERS or value in VALUE_WRAPPERS:
                index += 1
                continue
            if value.lower() in SHELLS and _depth < 3:
                # -c / -lc / -Command 之后那个带引号的词是一段子命令。
                cursor = index + 1
                takes_command = False
                while cursor < len(tokens) and tokens[cursor].value.startswith("-"):
                    option = tokens[cursor].value.lower().lstrip("-")
                    if "c" in option or option.startswith("command"):
                        takes_command = True
                    cursor += 1
                if takes_command and cursor < len(tokens) and tokens[cursor].quoted:
                    found.extend(sleep_calls(tokens[cursor].value, _depth + 1))
                index = cursor
                continue
            if is_sleep_word(value):
                seconds = None
                end = token.end
                cursor = index + 1
                if cursor < len(tokens) and tokens[cursor].value.lower().lstrip("-") == "seconds":
                    cursor += 1
                if cursor < len(tokens):
                    seconds = parse_duration(tokens[cursor].value)
                    if seconds is not None:
                        end = tokens[cursor].end
                if seconds is not None and command_position(tokens, index):
                    found.append({
                        "seconds": seconds,
                        "tail": tail_after(text, end),
                    })
                index = max(cursor, index + 1)
                continue
            index += 1
    return found


# ---------------------------------------------------------------- 日志与扫描


def cindy_home() -> Path:
    override = os.environ.get("CINDY_HOME")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Cindy"
    return Path.home() / "AppData" / "Roaming" / "Cindy"


def default_sessions_dir() -> Path:
    """会话日志目录：`%APPDATA%/Cindy/pi-agent-home/sessions`（可用 CINDY_HOME 覆盖）。"""
    return cindy_home() / "pi-agent-home" / "sessions"


def parse_time(value) -> datetime.datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def session_id_of(path: Path) -> str:
    matched = SESSION_ID.search(path.name)
    return matched.group(1) if matched else path.stem


def command_of(block: dict) -> str | None:
    """工具调用里的 shell 命令；没有 `arguments.command` 的调用一律跳过。"""
    arguments = block.get("arguments")
    if not isinstance(arguments, dict):
        return None
    command = arguments.get("command")
    return command if isinstance(command, str) else None


def tool_calls(record: dict):
    if record.get("type") != "message":
        return
    content = record.get("message", {}).get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "toolCall":
            yield block


def scan_file(path: Path, cutoff: datetime.datetime | None) -> tuple[list[dict], int, int]:
    """扫一份日志：返回（命中的 sleep 行、窗口内 sleep 命令条数、坏行条数）。"""
    calls: list[dict] = []
    sleep_commands = 0
    malformed = 0
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        raise AuditError(f"SESSIONS_DIR_NOT_FOUND：读不了日志 {path}")
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if not isinstance(record, dict):
                malformed += 1
                continue
            at = parse_time(record.get("timestamp"))
            if cutoff is not None and (at is None or at < cutoff):
                continue
            for block in tool_calls(record):
                command = command_of(block)
                if not command:
                    continue
                found = sleep_calls(command)
                if not found:
                    continue
                sleep_commands += len(found)
                for item in found:
                    if item["seconds"] < MIN_SLEEP_SECONDS:
                        continue
                    calls.append({
                        "session_id": session_id_of(path),
                        "session_file": path.name,
                        "timestamp": at.isoformat() if at else "",
                        "time": at.strftime("%Y-%m-%dT%H:%M:%SZ") if at else "-",
                        "seconds": item["seconds"],
                        "seconds_display": f"{item['seconds']:g}s",
                        "tail": item["tail"],
                        "tool": block.get("name") or "",
                        "command": re.sub(r"\s+", " ", command).strip()[:COMMAND_CHARS],
                    })
    return calls, sleep_commands, malformed


def scan(sessions_dir, hours: float = DEFAULT_HOURS, now=None) -> dict:
    """扫一遍目录，返回结构化结果（人读输出与 --json 都从这里出）。"""
    directory = Path(sessions_dir)
    if not directory.is_dir():
        raise AuditError(f"SESSIONS_DIR_NOT_FOUND：会话日志目录不存在：{directory}")
    now = datetime.datetime.now(datetime.timezone.utc) if now is None else now
    cutoff = None if hours <= 0 else now - datetime.timedelta(hours=hours)

    hits: list[dict] = []
    roster: list[dict] = []
    scanned = 0
    skipped = 0
    malformed = 0
    for path in sorted(directory.glob("*.jsonl")):
        if not path.is_file():
            continue
        # mtime 早于窗口起点的文件里不可能有窗口内的事件：整份跳过，省时间。
        if cutoff is not None:
            modified = datetime.datetime.fromtimestamp(
                path.stat().st_mtime, datetime.timezone.utc
            )
            if modified < cutoff:
                skipped += 1
                continue
        scanned += 1
        calls, sleep_commands, bad = scan_file(path, cutoff)
        malformed += bad
        if calls:
            hits.extend(calls)
        if sleep_commands:
            roster.append({
                "session_id": session_id_of(path),
                "session_file": path.name,
                "hits": len(calls),
                "sleep_commands": sleep_commands,
            })

    hits.sort(key=lambda item: (item["timestamp"], item["session_file"]), reverse=True)
    roster.sort(key=lambda item: (-item["hits"], -item["sleep_commands"],
                                  item["session_file"]))
    return {
        "generated_at": now.isoformat(),
        "sessions_dir": str(directory),
        "window_hours": hours,
        "min_seconds": MIN_SLEEP_SECONDS,
        "sessions_scanned": scanned,
        "sessions_skipped_by_mtime": skipped,
        "malformed_lines": malformed,
        "hits": hits,
        "sessions": roster,
        "summary": {
            "hits": len(hits),
            "sessions_with_hits": len({item["session_file"] for item in hits}),
            "sessions_with_sleep": len(roster),
            "sleep_commands": sum(item["sleep_commands"] for item in roster),
        },
    }


# ---------------------------------------------------------------- 输出


def render_human(result: dict) -> str:
    summary = result["summary"]
    window = "不限窗口" if result["window_hours"] <= 0 else f"{result['window_hours']:g} 小时"
    tail_lines = [
        f"合计：扫描 {result['sessions_scanned']} 个会话（跳过 {result['sessions_skipped_by_mtime']} 个"
        f"窗口外的）；有 sleep 命令 {summary['sessions_with_sleep']} 个"
        f"（其中命中 {summary['sessions_with_hits']} 个）；命中 {summary['hits']} 条"
        f"（阈值 {result['min_seconds']:g} 秒，窗口 {window}）"
    ]
    if not result["hits"]:
        # 没有命中就只留结论：把「发过 sleep 5」的会话也列出来，会让人误读成命中。
        return "\n".join([
            f"# 命中：≥{result['min_seconds']:g} 秒的 sleep —— 0 条",
            "（无命中）",
            *tail_lines,
        ])
    lines = [
        f"# 命中：≥{result['min_seconds']:g} 秒的 sleep，按时间倒序 —— "
        f"{summary['hits']} 条，分布在 {summary['sessions_with_hits']} 个会话",
    ]
    for item in result["hits"]:
        tail = item["tail"] or "（命令到此结束）"
        lines.append(
            f"{item['time']}  {item['seconds_display']:>6}  "
            f"{item['session_file']}  {tail}"
        )
    lines.append(
        f"# 会话一览：本窗口内出现过 sleep 命令的会话（含未达阈值）—— "
        f"{summary['sessions_with_sleep']} 个"
    )
    for item in result["sessions"]:
        lines.append(
            f"{item['session_file']}  命中 {item['hits']} / "
            f"sleep 命令 {item['sleep_commands']}"
        )
    lines.extend(tail_lines)
    return "\n".join(lines)


# ---------------------------------------------------------------- 入口


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sleep_audit.py",
        description="扫会话日志里的轮内 sleep：谁在本轮里睡等下游（只读）。",
        epilog="退出码：0 没有命中；1 有命中；2 参数或目录有问题。",
    )
    parser.add_argument(
        "--sessions-dir", default=None, metavar="PATH",
        help=f"会话日志目录；默认 {default_sessions_dir()}",
    )
    parser.add_argument(
        "--hours", type=float, default=DEFAULT_HOURS, metavar="N",
        help=f"只看最近 N 小时内的命令；0 表示不限（默认 {DEFAULT_HOURS:g}）",
    )
    parser.add_argument("--json", action="store_true", help="输出结构化结果")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:  # Windows 控制台默认 GBK，中文一打印就 UnicodeEncodeError
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    try:
        args = parse_args(sys.argv[1:] if argv is None else list(argv))
    except SystemExit as exc:  # --help 走这里，退出码 0；参数错是 2
        return int(exc.code or 0)

    if args.hours < 0:
        print("错误：--hours 不能为负数；0 表示不限窗口", file=sys.stderr)
        return 2

    directory = Path(args.sessions_dir) if args.sessions_dir else default_sessions_dir()
    try:
        result = scan(directory, hours=args.hours)
    except AuditError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_human(result))
    return 1 if result["hits"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
