#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中心库怎么被发现：优先级只有这一份，闸门都从这里取。

为什么要有这个文件：AWK-105 之前的每一处都自己写 `Path.home() / ".agents" / "center.json"`。
用 `install.py --agents-home <目录>` 装完之后，闸门照样去读 `~/.agents/center.json`，
于是它读到的是**另一套库**——实测读到 `D:/AI工作区` 的中心库并报 `SCHEMA_MISMATCH`。
库没坏，是找错了。同一件事写在几个文件里，就必然有几个答案。

顺序（写成一张表，也写进 `docs/closing.md` 的「中心库怎么被发现」）：

1. `--db`：人当场指定的那个；
2. `TASK_CENTER_DB`：钩子/脚本显式传的；
3. `TASK_CENTER_AGENTS_HOME`：显式指定用户级目录（测试与多份安装共用一台机器时用）；
4. **工作区指针** `<工作区>/.agents/center.json` 里的 `agents_home`，
   再读 `<agents_home>/center.json` 的 `db`——install.py 装的时候写这一个文件。
   指针指到的 `center.json` 不在就**到此为止**报 `DB_NOT_FOUND`：那说明这份安装的
   用户级目录被挪走或删了，回落到 `~/.agents` 只会读到另一套库（就是这个单的病根）；
5. `<工作区>/.data/task-center.sqlite3`：工作区自带的库（老安装没有指针，靠这一条；
   install.py 初始化的也正是这一份）；
6. `~/.agents/center.json`：默认用户级目录。默认安装（不传 `--agents-home`）走这一条，
   与 AWK-105 之前的行为逐字相同；
7. 都没有 → `DB_NOT_FOUND`。

工作树里第 4、5 步按 git 公共目录回到主检出：`.agents/` 与 `.data/` 都可能不进版本库，
工作树自己那份往往不存在，而它问的仍是同一个工作区。

**只发现，不新建。** 一份会话自己造一个库，会把记录悄悄拆成两份；这条从 AW-011 起
没变过，这个文件也不改它。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

# 工作区指针与工作区自带库的落点（相对工作区根）。只有这一处写死。
POINTER_RELATIVE = (".agents", "center.json")
DATABASE_RELATIVE = (".data", "task-center.sqlite3")
# 默认用户级目录（相对 HOME）。
DEFAULT_AGENTS_HOME = ".agents"

# 报告里反复要说的那句话：顺序 + 别新建库。
ORDER_HINT = (
    "发现顺序：--db → TASK_CENTER_DB → TASK_CENTER_AGENTS_HOME → "
    "工作区指针 .agents/center.json 的 agents_home → 本工作区 .data/task-center.sqlite3 → "
    "~/.agents。不要为了让命令跑起来新建一个库。"
)


class DiscoveryError(Exception):
    """找不到中心库，或者找到了一个「说得出为什么不在」的位置。

    带 `code` 是要紧的：调用方（闸门、钩子）把它原样打进输出与非零退出，
    而不是降级成一句「没有数据」。
    """

    code = "DB_NOT_FOUND"

    def __init__(self, message, fix=None, config=None):
        super().__init__(message)
        self.message = message
        self.fix = fix or ORDER_HINT
        self.config = Path(config) if config is not None else None

    def as_payload(self):
        payload = {"error": self.code, "fix": f"{self.message}。{self.fix}"}
        if self.config is not None:
            payload["config"] = str(self.config)
        return payload


class Resolution:
    """一次成功发现的结果：库在哪、凭什么找到的。"""

    def __init__(self, db, source, config=None, agents_home=None):
        self.db = Path(db)
        self.source = source
        self.config = Path(config) if config is not None else None
        self.agents_home = Path(agents_home) if agents_home is not None else None

    def __repr__(self):  # 便于 --list / 测试里看清楚「凭什么」
        return f"Resolution(db={self.db!s}, source={self.source!r})"


def default_agents_home(home=None):
    """默认用户级目录：`~/.agents`。`home` 只给测试替换。"""
    base = Path(home) if home is not None else Path.home()
    return base / DEFAULT_AGENTS_HOME


def main_checkout(repo):
    """工作树 → 主检出；不是工作树、没有 git、或 git 认不出就返回 None。"""
    repo = Path(repo).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or "").strip()
    if result.returncode != 0 or not output:
        return None
    common = Path(output)
    if not common.is_absolute():
        common = (repo / common).resolve()
    # 主检出的 `.git` 就是公共目录；工作树共用它，所以公共目录的父目录是主检出。
    parent = common.parent
    if common.name != ".git" or parent == repo:
        return None
    return parent


def workspace_roots(repo):
    """要依次看的工作区根：本检出在前，主检出在后（去重、保序）。"""
    roots = []
    for candidate in (Path(repo).resolve(), main_checkout(repo)):
        if candidate is None:
            continue
        if all(str(candidate).casefold() != str(item).casefold() for item in roots):
            roots.append(candidate)
    return roots


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _from_agents_home(agents_home, source, config=None):
    """用户级目录 → 它下面的 center.json → db。文件不在就报错，不往下找。"""
    agents_home = Path(agents_home).expanduser()
    config = Path(config) if config is not None else agents_home / "center.json"
    if not config.is_file():
        raise DiscoveryError(
            f"{source}指向的用户级配置不存在：{config}",
            fix=("不回落到 ~/.agents——那是另一套库（这正是 AWK-105 报 SCHEMA_MISMATCH 的原因）。"
                 "要么重新跑 install.py --agents-home 把这份配置写回来，"
                 "要么显式给 --db / TASK_CENTER_DB；不要手工新建一个库。"),
            config=config,
        )
    data = _read_json(config)
    if data is None:
        raise DiscoveryError(f"读不懂用户级配置：{config}", config=config)
    declared = data.get("db")
    if not isinstance(declared, str) or not declared.strip():
        raise DiscoveryError(f"用户级配置没有 db 字段：{config}", config=config)
    return Resolution(Path(declared).expanduser(), source, config=config, agents_home=agents_home)


def resolve(repo, explicit=None, env=None, home=None, roots=None, include_user_config=True):
    """按 ORDER_HINT 的顺序找中心库，返回 `Resolution`；找不到抛 `DiscoveryError`。

    `repo` 是「问问题的那份代码/仓库住在哪」（工具的 REPO，或 `--repo`）；
    `env` / `home` / `roots` 只为测试留口子，生产调用方不传。
    """
    env = os.environ if env is None else env
    if explicit:
        return Resolution(explicit, "argument")
    env_db = (env.get("TASK_CENTER_DB") or "").strip()
    if env_db:
        return Resolution(env_db, "TASK_CENTER_DB")
    env_home = (env.get("TASK_CENTER_AGENTS_HOME") or "").strip()
    if env_home:
        return _from_agents_home(env_home, "TASK_CENTER_AGENTS_HOME")
    roots = list(roots) if roots is not None else workspace_roots(repo)
    # 第 4 步：工作区指针。先把所有根看完，再谈回落——工作树自己那份 .agents/ 可能
    # 不在版本库里，但主检出那份在，它们问的是同一个工作区。
    for root in roots:
        pointer = Path(root).joinpath(*POINTER_RELATIVE)
        if not pointer.is_file():
            continue
        data = _read_json(pointer)
        if data is None:
            raise DiscoveryError(f"读不懂工作区指针：{pointer}", config=pointer)
        agents_home = data.get("agents_home")
        if isinstance(agents_home, str) and agents_home.strip():
            return _from_agents_home(agents_home, f"工作区指针 {pointer}", config=None)
        declared = data.get("db")
        if isinstance(declared, str) and declared.strip():
            # 手写的老形状：指针里直接放 db。照用，但不再有 agents-home 可以核对。
            return Resolution(Path(declared).expanduser(), f"工作区指针 {pointer}", config=pointer)
        raise DiscoveryError(f"工作区指针既没有 agents_home 也没有 db：{pointer}", config=pointer)
    # 第 5 步：工作区自带的库。
    for root in roots:
        candidate = Path(root).joinpath(*DATABASE_RELATIVE)
        if candidate.is_file():
            return Resolution(candidate, f"工作区库 {candidate}")
    # 第 6 步：默认用户级目录（默认安装走这里，行为与以前逐字相同）。
    if include_user_config:
        return _from_agents_home(default_agents_home(home), "默认用户级目录 ~/.agents")
    raise DiscoveryError(
        f"{Path(repo).resolve()} 既没有工作区指针（.agents/center.json），也没有自带的中心库。",
        fix=ORDER_HINT,
    )
