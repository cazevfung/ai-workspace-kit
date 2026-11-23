#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 `plugin/draft/` 打成 `dist/ai-workspace-kit-<版本>.cindy`。

为什么要有这个脚本：`.cindy` 就是一个 zip，但「手搓一个 zip」和「打出来的包能装」
之间隔着三件事——包根必须是 `ghost.json`（不能套一层目录）、清单里的 `entry` 与 `icon`
必须真在包里、包里的版本必须与源码和 `VERSION` 一致。这三件以前靠一次性命令，
改一版就得再想一遍命令怎么写；现在是一条命令，判据落在退出码上。

另外两条刻意的口径：

1. **同样的源码打出来的字节相同。** 条目按名字排序、时间戳固定成 1980-01-01、
   压缩级别固定，所以 `dist/` 下的包不会因为「今天几点打的」而变化——它进版本库，
   每次发布都多一个几 MB 的二进制差异是没必要的。
2. **打完先拆开读一遍。** 写完立刻重新打开、读 `ghost.json`、核对入口与图标的名字
   与字节数，再删掉 `dist/` 下的旧包。写成功不等于包能装。

用法（在仓库根跑）：

    python tools/pack_plugin.py

退出码：0 打好；1 打包过程中发现包不合格；2 版本号对不上或少了源文件。
"""
from __future__ import annotations

import json
import struct
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "plugin" / "draft"
DIST = ROOT / "dist"
VERSION_FILE = ROOT / "VERSION"
# 固定时间戳：1980-01-01 00:00 是 zip 能表示的最早时刻。用它而不是当前时间，
# 是为了让「同一份源码」打出来的包逐字节相同。
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def fail(message: str) -> None:
    print(f"× {message}", file=sys.stderr)


def png_size(data: bytes):
    """PNG 的 IHDR 紧跟 8 字节签名：宽高各 4 字节大端。"""
    if len(data) < 24 or data[:8] != PNG_SIGNATURE:
        return None
    return struct.unpack(">II", data[16:24])


def package_name(version: str) -> str:
    return f"ai-workspace-kit-{version}.cindy"


def source_files() -> list[Path]:
    """`plugin/draft/` 下的全部文件，按 posix 相对路径排序（打出来的条目顺序固定）。"""
    files = []
    for path in sorted(SOURCE.rglob("*")):
        rel = path.relative_to(SOURCE)
        if path.is_dir() or "__pycache__" in rel.parts or path.suffix == ".pyc":
            continue
        files.append(path)
    return sorted(files, key=lambda p: p.relative_to(SOURCE).as_posix())


def check_source(version: str, manifest: dict) -> list[str]:
    problems = []
    for key in ("id", "version", "entry", "icon"):
        if not manifest.get(key):
            problems.append(f"ghost.json 缺 `{key}`")
    if manifest.get("version") != version:
        problems.append(f"ghost.json 的 version 是 {manifest.get('version')!r}，VERSION 是 {version!r}")
    entry = manifest.get("entry")
    if entry and not (SOURCE / entry).is_file():
        problems.append(f"清单声明的入口 {entry} 不在 plugin/draft/ 里")
    icon = manifest.get("icon")
    if icon and not (SOURCE / icon).is_file():
        problems.append(f"清单声明的图标 {icon} 不在 plugin/draft/ 里")
    return problems


def write_package(target: Path, files: list[Path]) -> None:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(SOURCE).as_posix(), date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())


def verify_package(target: Path) -> list[str]:
    """拆开读一遍：能读、包根有清单、入口非空、图标是真 PNG。"""
    problems = []
    with zipfile.ZipFile(target) as archive:
        broken = archive.testzip()
        if broken is not None:
            return [f"包里的 {broken} 读不出来"]
        names = set(archive.namelist())
        if "ghost.json" not in names:
            return ["包根没有 ghost.json（不能套一层目录）"]
        manifest = json.loads(archive.read("ghost.json").decode("utf-8"))
        entry = manifest.get("entry")
        if entry not in names:
            problems.append(f"清单声明的入口 {entry} 不在包里")
        elif archive.getinfo(entry).file_size == 0:
            problems.append(f"入口 {entry} 是空文件")
        icon = manifest.get("icon")
        if icon not in names:
            problems.append(f"清单声明的图标 {icon} 不在包里")
        else:
            data = archive.read(icon)
            size = png_size(data)
            if size is None:
                problems.append(f"图标 {icon} 不是 PNG")
            elif size[0] != size[1]:
                problems.append(f"图标应当是方图，实际 {size[0]}x{size[1]}")
    return problems


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv:
        print(__doc__.strip())
        return 2

    if not VERSION_FILE.is_file():
        fail(f"找不到 {VERSION_FILE}")
        return 2
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not version:
        fail("VERSION 是空的")
        return 2

    manifest_path = SOURCE / "ghost.json"
    if not manifest_path.is_file():
        fail(f"找不到 {manifest_path}")
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    problems = check_source(version, manifest)
    if problems:
        for problem in problems:
            fail(problem)
        return 2

    files = source_files()
    if not files:
        fail(f"{SOURCE} 里一个文件都没有")
        return 2

    DIST.mkdir(parents=True, exist_ok=True)
    target = DIST / package_name(version)
    stale = sorted(p for p in DIST.glob("*.cindy") if p != target)

    write_package(target, files)
    problems = verify_package(target)
    if problems:
        for problem in problems:
            fail(problem)
        target.unlink(missing_ok=True)
        return 1

    # 新包验过了才删旧包：中途失败时 dist/ 里至少还有一个能装的包。
    for path in stale:
        path.unlink()
        print(f"   删掉旧包：{path.name}")

    print(f"已打包：{target.relative_to(ROOT).as_posix()}")
    print(f"   id / version：{manifest.get('id')} / {manifest.get('version')}")
    print(f"   条目 {len(files)} 个，{target.stat().st_size} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
