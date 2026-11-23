#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""这次安装允不允许写用户级配置：判据只有这一处。

为什么要有这个文件：2026-11-23 22:10，一个验收会话把工作区装到临时目录
`D:\\awk109-verify\\ws`，`--agents-home` 用了默认值（也就是这台机器上真实的
`~/.agents`）。安装器当时无条件写 `<agents_home>/center.json`，于是真实
`center.json` 被改指到沙箱的空库——那一刻这台机器上所有工作区工具一起读到空库，
报「0 张在途、0 张真待合并」。库没坏，是安装把一个还在用的指针静默改掉了。

判据两条，都只看事实、不看语气：

1. **agents_home 落在真实用户目录以外**（沙箱 / 临时 / 别人的目录）：默认**不写**
   任何用户级文件（`center.json`、`skills/`、`intake/`、安装清单）。要写必须显式
   `--allow-user-config`——「装到临时目录试一下」不该有副作用。
2. **agents_home 就是真实用户目录，而那里的 `center.json` 已经指向别的库**：
   默认**拒绝**并让调用方把原内容原样打印出来。要重指必须显式 `--allow-user-config`
   ——这正是那天发生的事，只是这次在写盘之前就停住。

首次安装（`center.json` 不存在）与「同一份库再装一次」行为不变：照写。
`tree_digest` 是配套的只读工具：装前装后对同一棵树取快照，用来证明「一个字节没变」，
不必先写进去再恢复（真实 `~/.agents` 有 3000 多个文件、500 MB 上下，按内容哈希
小文件、按大小与 mtime 记大文件，一次约半秒）。

本模块只依赖标准库、只读文件系统，不写任何东西。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_AGENTS_HOME = ".agents"
CENTER_NAME = "center.json"
# 内容哈希的上限：再大的文件只记大小与 mtime（快照要能在用例里跑得动）。
CONTENT_LIMIT = 256 * 1024

# 判定结果的 reason 取值。字面量固定下来，用例与安装器的输出都按它说话。
REASON_SANDBOX_SKIPPED = "sandbox-skipped"
REASON_EXPLICIT_ALLOW = "explicit-allow"
REASON_FIRST_INSTALL = "first-install"
REASON_SAME_DB = "same-db"
REASON_WOULD_REPOINT = "would-repoint"


def real_agents_home(home=None) -> Path:
    """本机真实的用户级目录。`home` 可注入：用例用假 HOME 测「真装」那条路。"""
    base = Path(home) if home is not None else Path.home()
    return base / DEFAULT_AGENTS_HOME


def _same_path(left, right) -> bool:
    """同一个位置的不同写法（正/反斜杠、大小写、软链）算同一个。"""
    if not left or not right:
        return False
    a = os.path.normcase(os.path.realpath(str(left)))
    b = os.path.normcase(os.path.realpath(str(right)))
    return a == b


def read_center(path) -> tuple[str, str]:
    """读一份 `center.json`，返回 `(db, 原文)`。

    读不到、不是 UTF-8、不是 JSON 对象、没有字符串 `db` 时 `db` 是空串，
    原文照抄（能读到多少算多少）。调用方据此决定「说不清是不是我的 → 拒」。
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "", ""
    try:
        data = json.loads(text)
    except ValueError:
        return "", text
    if not isinstance(data, dict):
        return "", text
    db = data.get("db")
    return (db.strip() if isinstance(db, str) else ""), text


@dataclass(frozen=True)
class UserConfigDecision:
    """一次安装对用户级配置的结论。`refusal` 非空表示「真装会停在这里」。"""

    allowed: bool
    sandbox: bool
    reason: str
    refusal: str
    existing_db: str
    existing_text: str
    agents_home: Path
    workspace_db: Path
    real_agents_home: Path

    @property
    def center_path(self) -> Path:
        return self.agents_home / CENTER_NAME


def decide_user_config(agents_home, workspace_db, allow_user_config: bool = False,
                       home=None) -> UserConfigDecision:
    """这次安装能不能写用户级配置。

    判序（先「能不能碰」再「会不会改别人的指针」）：

    1. 沙箱且没给开关 → 跳过（`allowed=False`, `reason=sandbox-skipped`, 不算拒绝）；
    2. `center.json` 存在且 db 与本工作区库不同 → 没开关就拒绝（`would-repoint`），
       给了开关就覆盖（`explicit-allow`）；
    3. 其余：给了开关、同一个库（`same-db`）、或首次安装（`first-install`）都照写。
    """
    agents = Path(agents_home).expanduser()
    real = real_agents_home(home).expanduser()
    ws_db = Path(workspace_db)
    center = agents / CENTER_NAME
    existing_db, existing_text = read_center(center)
    sandbox = not _same_path(agents, real)
    allowed, reason, refusal = False, "", ""

    if sandbox and not allow_user_config:
        reason = REASON_SANDBOX_SKIPPED
    elif center.exists() and not _same_path(existing_db, ws_db):
        if allow_user_config:
            allowed, reason = True, REASON_EXPLICIT_ALLOW
        else:
            reason = REASON_WOULD_REPOINT
            pointed = existing_db or f"读不出 db 字段（原文 {len(existing_text)} 字节）"
            refusal = (
                f"{center} 已经指向另一个库：{pointed}。"
                f"直接改写会让那套工作区一起读到本工作区的库 {ws_db}，"
                "所以默认一个字节都不写。确认要重指，请在命令里加 --allow-user-config 再跑一次。"
            )
    elif allow_user_config:
        allowed, reason = True, REASON_EXPLICIT_ALLOW
    elif center.exists():
        allowed, reason = True, REASON_SAME_DB
    else:
        allowed, reason = True, REASON_FIRST_INSTALL

    return UserConfigDecision(
        allowed=allowed, sandbox=sandbox, reason=reason, refusal=refusal,
        existing_db=existing_db, existing_text=existing_text,
        agents_home=agents, workspace_db=ws_db, real_agents_home=real,
    )


def tree_digest(root) -> dict:
    """整棵树的只读快照：`相对路径 → 指纹`。

    目录各记一条（键以 `/` 结尾），文件算内容哈希；超过 `CONTENT_LIMIT` 的只记
    `size` 与 `mtime_ns`（500 MB 的用户级目录按内容哈希全量算太贵，而安装器会写的
    都是小文件）。根目录自身记一个 `.` 键，这样「原本不存在」与「新建了一个空目录」
    也是两回事。
    """
    root = Path(root)
    if not root.is_dir():
        return {}
    digest = {".": "dir"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        for name in dirnames:
            key = (Path(dirpath) / name).relative_to(root).as_posix() + "/"
            digest[key] = "dir"
        for name in filenames:
            path = Path(dirpath) / name
            key = path.relative_to(root).as_posix()
            try:
                stat = path.stat()
            except OSError:
                digest[key] = "unreadable"
                continue
            if stat.st_size <= CONTENT_LIMIT:
                try:
                    digest[key] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                    continue
                except OSError:
                    pass
            digest[key] = f"size:{stat.st_size}:mtime:{stat.st_mtime_ns}"
    return digest


def diff_tree(before: dict, after: dict) -> list[str]:
    """两份快照之间的差异路径（改了、多了、少了都算），按字母序。"""
    keys = set(before) | set(after)
    return sorted(key for key in keys if before.get(key) != after.get(key))
