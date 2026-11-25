"""Resolve and fingerprint local project files; not an OS-level sandbox."""
import hashlib
import os
from pathlib import Path

from .db import Problem, require

MAX_FILE_BYTES = 32 * 1024 * 1024

# `submissions.artifacts` 的每一项恰好三个键，顺序不限定（AW-159）。
# 这三个键各自回答一件事：`path` 是哪份文件、`sha256` 是提交那一刻工作树里那份字节、
# `size` 是它的字节数。指纹比对按整份字典逐项相等来判，所以多出来的键不是备注、
# 而是永远对不上的理由：HSE-008 的提交 1cd60777 多了一个 `role`，验收每次都在
# `ARTIFACT_CHANGED` 上停住，而那句话既没说是哪条提交、也没说是哪个键——指不到
# 具体位置的门和没有门是一样的。
ARTIFACT_KEYS = ("path", "sha256", "size")


def check_artifact(value, where):
    """一项 artifact 的键集合必须恰好是 `ARTIFACT_KEYS`，否则报 `ARTIFACT_KEYS`。

    `where` 是给人看的位置串（哪条提交的第几项），必须由调用方拼好传进来：
    报错里「多出的键是 role」只说了是什么，加上「submission 1cd60777 的 artifacts[0]」
    才说了在哪里。两道写边各调一次——`submit_result` 写完指纹立刻查（把字段名告诉
    提交者），`review_result` 比指纹之前查（把提交、项、键、期望值告诉验收人）。
    """
    require(isinstance(value, dict), "ARTIFACT_KEYS",
            f"{where} 不是对象（而是 {type(value).__name__}）；"
            f"artifacts 的每一项必须是恰好含 {', '.join(ARTIFACT_KEYS)} 三键的对象")
    missing = [key for key in ARTIFACT_KEYS if key not in value]
    extra = [key for key in value if key not in ARTIFACT_KEYS]
    if not missing and not extra:
        return value
    detail = []
    if extra:
        detail.append(f"多出的键：{', '.join(extra)}")
    if missing:
        detail.append(f"缺少的键：{', '.join(missing)}")
    raise Problem("ARTIFACT_KEYS",
                  f"{where} 的键集合是 {sorted(value)}，只允许 {' / '.join(ARTIFACT_KEYS)} 三键；"
                  + "；".join(detail)
                  + "。指纹比对按整份字典逐项比对，多一个键就永远对不上，表现成一句无解的 "
                    "ARTIFACT_CHANGED；修这一行（或 recover 之后重新提交）再验收")


def project_root(value):
    require(isinstance(value, str) and bool(value.strip()), "INVALID_INPUT", "root must be a nonempty string")
    path = Path(value)
    require(path.is_absolute(), "INVALID_INPUT", "Project root must be absolute")
    require(path.is_dir(), "FILE_NOT_FOUND", "Project directory does not exist")
    return os.path.normcase(str(path.resolve()))


def fingerprint(root, relative):
    require(isinstance(relative, str) and bool(relative.strip()), "INVALID_INPUT", "File path must be a nonempty string")
    raw = Path(relative)
    require(not raw.is_absolute() and not raw.drive, "PATH_OUTSIDE_PROJECT", "Use a project-relative file path")
    base = Path(root).resolve()
    target = (base / raw).resolve()
    require(target.is_relative_to(base), "PATH_OUTSIDE_PROJECT", "File resolves outside the project")
    require(target.is_file(), "FILE_NOT_FOUND", "Referenced project file does not exist")
    digest = hashlib.sha256()
    size = 0
    try:
        with target.open("rb") as source:
            before = os.fstat(source.fileno())
            require(before.st_size <= MAX_FILE_BYTES, "FILE_TOO_LARGE", "File exceeds the 32 MiB first-version limit")
            while chunk := source.read(65536):
                size += len(chunk)
                require(size <= MAX_FILE_BYTES, "FILE_TOO_LARGE", "File exceeds the 32 MiB first-version limit")
                digest.update(chunk)
            after = os.fstat(source.fileno())
            require((before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size),
                    "FILE_CHANGED", "File changed while being read")
    except OSError as exc:
        raise Problem("FILE_UNAVAILABLE", "Cannot read referenced project file") from exc
    return {"path": target.relative_to(base).as_posix(), "sha256": digest.hexdigest(), "size": size}


def artifact_drift(saved, current):
    """指纹对不上时，指出是哪几个键变了，而不是只说「变了」。

    返回一串「键 提交时 X 现在 Y」。三个键都在，所以这句话永远指得到位置：
    同一个 `ARTIFACT_CHANGED` 里，`size` 变了与 `sha256` 变了是两件事（前者通常是被
    改写或截断，后者常见的是换行口径，见 `docs/protocol.md` 的「artifact 的 SHA-256
    与 git blob 的 sha 不是同一个数」）。
    """
    return "，".join(f"{key} 提交时 {saved[key]!r} 现在是 {current[key]!r}"
                     for key in ARTIFACT_KEYS if saved.get(key) != current.get(key))
