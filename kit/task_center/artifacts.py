"""Resolve and fingerprint local project files; not an OS-level sandbox."""
import hashlib
import os
from pathlib import Path

from .db import Problem, require

MAX_FILE_BYTES = 32 * 1024 * 1024


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
