"""只读的宿主会话解析：`session_id` → `{device, state}`。

为什么要有这一层：一张单牵扯到的会话标识都在本工具的库里（`attempts.session_id`、
`reviews.reviewer_session_id`……），而「这台机器是哪台」与「会话还在不在」只有宿主的库知道
（`sessions.agent_device_id` / `sessions.status`）。把链接拼成 `cindy://session/<id>?device=<id>`
需要后者，但本工具不该因此变成宿主的写入口：本模块只读、只答、读不到就答空表。

三条硬口径：

- **只读。** `mode=ro` + `PRAGMA query_only`，不建库、不写行、不改任何会话。
- **读不到就是空表，不抛异常。** 宿主库不存在、不是 sqlite、没有 `sessions` 表，一律 `{}`；
  调用方据此把 `device` 写成 `null`、把状态写成 `unknown`，而不是猜一个值。
- **本机取不到 device 是常态。** 本机 1966 条会话的 `agent_device_id` 全是 NULL，
  所以「device 缺失」不是一个假想分支，是默认分支。

路径选择：环境变量 `TASK_CENTER_CINDY_DB` 覆盖；否则取 `%APPDATA%/Cindy/` 下 mtime 最新的
`cindy-*.db`（与 `tools/model_report.py` 同一个取法）。缓存按「路径 + mtime」失效，所以同一个
进程里反复调 `get_ticket` 只读一次。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

OVERRIDE_ENV = "TASK_CENTER_CINDY_DB"

_CACHE: dict[tuple[str, int], dict[str, dict]] = {}


def cindy_dir() -> Path:
    """宿主库所在目录：从环境变量拼，不写死用户名。"""
    roaming = os.environ.get("APPDATA")
    if not roaming:
        roaming = str(Path.home() / "AppData" / "Roaming")
    return Path(roaming) / "Cindy"


def newest_database(directory: Path | None = None) -> Path | None:
    """取目录下 mtime 最新的 `cindy-*.db`；一个都没有时返回 None。"""
    folder = cindy_dir() if directory is None else Path(directory)
    if not folder.is_dir():
        return None
    candidates = sorted(
        (item for item in folder.glob("cindy-*.db") if item.is_file()),
        key=lambda item: (item.stat().st_mtime, item.name),
    )
    return candidates[-1] if candidates else None


def resolve_path() -> Path | None:
    """这次该读哪个宿主库：环境变量优先，否则取最新的一份。"""
    override = os.environ.get(OVERRIDE_ENV)
    if override:
        return Path(override).expanduser()
    return newest_database()


def host_sessions(path=None) -> dict[str, dict]:
    """返回 `{session_id: {"device": str|None, "state": str}}`，读不到就是 `{}`。"""
    target = resolve_path() if path is None else Path(path)
    if target is None or not target.is_file():
        return {}
    try:
        stamp = target.stat().st_mtime_ns
    except OSError:
        return {}
    key = (str(target), stamp)
    cached = _CACHE.get(key)
    if cached is None:
        cached = _read(target)
        _CACHE[key] = cached
    return dict(cached)


def _read(target: Path) -> dict[str, dict]:
    # as_uri() 负责把 Windows 路径与中文目录名转成 sqlite 认的 URI；再挂只读参数。
    try:
        conn = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=5)
    except (sqlite3.Error, ValueError):
        return {}
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "sessions" not in tables:
            return {}
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "id" not in columns:
            return {}
        wanted = [name for name in ("id", "status", "agent_device_id") if name in columns]
        rows = conn.execute(f"SELECT {','.join(wanted)} FROM sessions").fetchall()
    except (sqlite3.Error, ValueError, OSError):
        return {}
    finally:
        conn.close()
    answer = {}
    for row in rows:
        session_id = row["id"]
        if not isinstance(session_id, str) or not session_id.strip():
            continue
        device = row["agent_device_id"] if "agent_device_id" in wanted else None
        state = row["status"] if "status" in wanted else None
        answer[session_id] = {
            "device": device.strip() if isinstance(device, str) and device.strip() else None,
            "state": state.strip() if isinstance(state, str) and state.strip() else "unknown",
        }
    return answer
