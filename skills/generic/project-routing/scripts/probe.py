"""Read-only project presence probe.

It reports facts about which project directories exist, when they were last
touched, and which registered projects have gone missing. It performs no
matching, keeps no cache, writes nothing, and never decides which project an
utterance refers to.

Design notes that matter:

* Directory mtime is deliberately not used. A directory's mtime only changes
  when its direct children are added or removed, so editing files deep inside a
  project leaves the parent timestamp untouched. Git history is the preferred
  signal; a bounded file walk is the fallback.
* One git call per repository. Process spawn dominates the cost on Windows
  (measured ~85 ms each), so the working-tree status call is skipped: it would
  double the cost for a weak signal.
* A registration whose root has disappeared is reported as broken instead of
  being silently dropped. Silent omission is the main failure mode of derived
  information.
* A git call that times out is reported as unobserved, never as active.
  Treating a timeout as activity would invert the meaning of the signal.
* Ranking is truncated. A long list costs the caller tokens forever; the full
  count stays visible in `counts` so nothing looks complete when it is not.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

MAX_SCAN_FILES = 2000
GIT_TIMEOUT = 5.0
GIT_LOG_LIMIT = 200
RECENT_WINDOW_DAYS = 30
DEFAULT_DEPTH = 2
DEFAULT_ACTIVE_DAYS = 14
DEFAULT_LIMIT = 8
DEFAULT_JOBS = 8

# A directory counts as a project candidate when it carries one of these
# markers. The list is reported back in --explain so a stale rule is visible.
MARKERS = ("README.md", "PRD", ".worktrees", "package.json", "pyproject.toml", "AGENTS.md", "CLAUDE.md")
NOISE = {"node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".cache", ".idea", ".vscode",
         "target", "obj", "bin", ".pytest_cache", ".mypy_cache", "site-packages", "$RECYCLE.BIN",
         "System Volume Information"}
BANDS = {"active": 0, "unobserved": 1, "dormant": 2}


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds") if epoch else None


def norm(value):
    return os.path.normcase(str(Path(value).resolve()))


def ago(epoch):
    days = (time.time() - epoch) / 86400
    return f"{int(days * 24)}h ago" if days < 1 else f"{int(days)}d ago"


def markers(root):
    return [name for name in MARKERS if (root / name).exists()]


def git(root, args):
    """Run one bounded git command. Returns None on timeout, '' on failure."""
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=GIT_TIMEOUT,
                              encoding="utf-8", errors="replace")
    except (subprocess.TimeoutExpired, OSError):
        return None
    return done.stdout if done.returncode == 0 else ""


def git_facts(root):
    """One call yields both last-commit time and the recent-commit count."""
    if not (root / ".git").exists():
        return {"is_git": False, "unobserved": False}
    raw = git(root, ["log", f"-n{GIT_LOG_LIMIT}", "--format=%ct", f"--since={RECENT_WINDOW_DAYS * 4} days ago"])
    if raw is None:
        return {"is_git": True, "unobserved": True, "reason": "git timed out"}
    stamps = [int(line) for line in raw.split() if line.isdigit()]
    if not stamps:
        # Older than the window: one more, still bounded, call.
        raw = git(root, ["log", "-1", "--format=%ct"])
        if raw is None:
            return {"is_git": True, "unobserved": True, "reason": "git timed out"}
        stamps = [int(line) for line in raw.split() if line.isdigit()]
    cutoff = time.time() - RECENT_WINDOW_DAYS * 86400
    return {"is_git": True, "unobserved": False,
            "last_commit": max(stamps) if stamps else None,
            "recent_commits": sum(1 for value in stamps if value >= cutoff)}


def tree_facts(root, depth):
    """Bounded walk for projects without git, and for empty-shell detection."""
    newest, found, scanned = None, 0, 0
    base = len(root.parts)
    for current, dirs, names in os.walk(root, followlinks=False):
        here = Path(current)
        if len(here.parts) - base >= depth:
            dirs[:] = []
        dirs[:] = [name for name in dirs if name not in NOISE and not name.startswith(".")
                   and not (here / name).is_symlink()]
        for name in names:
            scanned += 1
            if scanned > MAX_SCAN_FILES:
                return {"last_file": newest, "scanned": scanned, "truncated": True}
            try:
                mtime = (here / name).stat().st_mtime
            except OSError:
                continue
            found += 1
            newest = mtime if newest is None else max(newest, mtime)
    return {"last_file": newest, "scanned": scanned, "found": found, "truncated": False}


def folded(path):
    """Comparable path parts: resolved, then case-folded.

    Both halves matter on Windows. Registered roots arrive normcased by project_root()
    while scanned ones keep the on-disk spelling, and resolve() turns an 8.3 short name
    into the long form the registration stored. Comparing raw parts gets both directions
    wrong, which is how a registered project swallowed every project nested inside it.
    """
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path)
    return tuple(part.casefold() for part in Path(os.path.normcase(str(resolved))).parts)


def describe(root, depth, active_days):
    """One item: facts only, no ranking and no interpretation."""
    if not root.is_dir():
        return {"root": str(root), "state": "broken", "signals": ["root is not a readable directory"]}

    item = {"root": str(root), "name": root.name, "state": "candidate", "signals": [],
            "markers": markers(root)}
    history = git_facts(root)
    if history["unobserved"]:
        item.update({"activity": "unobserved", "last_touch": None})
        item["signals"].append(history["reason"])
        return item

    last = None
    if history["is_git"]:
        last = history["last_commit"]
        item["git"] = {"last_commit": iso(last), "recent_commits": history["recent_commits"]}
        if last is None:
            item["signals"].append("git repo without commits")
        else:
            item["signals"].append(f"last commit {ago(last)}")
            if history["recent_commits"]:
                item["signals"].append(f"{history['recent_commits']} commits in {RECENT_WINDOW_DAYS}d")
    else:
        walked = tree_facts(root, depth)
        item["signals"].append("no git; bounded file walk")
        last = walked["last_file"]
        if walked["truncated"]:
            item["signals"].append(f"walk stopped after {MAX_SCAN_FILES} files")
        if last is None:
            item["signals"].append("no files found")
        else:
            item["signals"].append(f"newest file {ago(last)}")
            item["files_found"] = walked.get("found", 0)

    item["last_touch"] = iso(last)
    if last is None:
        # Present but empty: the classic shell that looks like a project.
        item.update({"activity": "dormant", "empty_shell": True})
        item["signals"].append("empty shell")
    elif time.time() - last <= active_days * 86400:
        item["activity"] = "active"
    else:
        item["activity"] = "dormant"
    return item


def candidate_dirs(root, depth):
    """Marked directories under root, bounded by depth. The root qualifies only if marked."""
    if markers(root):
        yield root
    base = len(root.parts)
    for current, dirs, _ in os.walk(root, followlinks=False):
        here = Path(current)
        if len(here.parts) - base >= depth:
            dirs[:] = []
        dirs[:] = [name for name in dirs if name not in NOISE and not name.startswith(".")
                   and not (here / name).is_symlink()]
        for name in sorted(dirs):
            child = here / name
            tags = markers(child)
            if tags:
                yield child


def registered(db_path):
    """Read registered projects without opening the database for writing."""
    if db_path is None:
        return {"available": False, "reason": "no database supplied", "items": []}
    path = Path(db_path).absolute()
    if not path.is_file():
        return {"available": False, "reason": "database absent", "items": []}
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute("SELECT id, name, root FROM projects ORDER BY id").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"available": False, "reason": f"read-only query failed: {exc}", "items": []}
    return {"available": True, "items": [{"project_id": row[0], "name": row[1], "root": row[2]} for row in rows]}


def order(items):
    """Activity bands first, most recent inside each band. Time only sorts; it never promotes."""
    def key(item):
        touched = item.get("last_touch")
        return (BANDS.get(item.get("activity"), 3),
                -(datetime.fromisoformat(touched).timestamp() if touched else 0.0))
    return sorted(items, key=key)


def mark_nested(items):
    """Flag items whose ancestor is also listed. Facts only, but it stops one project being
    offered twice when a subdirectory happens to carry a marker as well.

    A registered project is never suppressed. It is an explicit entity, not a guess, and
    every registered project living under another registered root (a workspace with
    projects inside it) would otherwise disappear from the ranking entirely.
    """
    # Registered roots come back from the database already normcased; scanned ones keep the
    # on-disk spelling. Comparing raw parts makes both directions wrong on Windows, so the
    # comparison runs on case-folded parts.
    # The coordinator is excluded from the listing, so it must not silence anything either:
    # a directory that is never offered cannot legitimately swallow another candidate.
    roots = [folded(item["root"]) for item in items
             if item["state"] != "broken" and item.get("role") != "coordinator"]
    for item in items:
        if item["state"] in ("broken", "registered"):
            item.setdefault("nested", False)
            continue
        parts = folded(item["root"])
        item["nested"] = any(len(other) < len(parts) and parts[:len(other)] == other for other in roots)
    return items


DEFAULT_CONFIG = Path.home() / ".agents" / "center.json"


def detect_here(start=None, stop=None):
    """Where this session already is. No scanning: only the current chain is inspected.

    A repository root wins over a marker directory, because a project that is a repo is
    that repo even when a subdirectory also happens to carry a README. Otherwise the
    nearest directory carrying a project marker wins.
    """
    cwd = Path(os.getcwd() if start is None else start).resolve()
    if not cwd.is_dir():
        cwd = cwd.parent
    chain = [cwd, *cwd.parents]
    for path in chain:
        if (path / ".git").exists():
            return {"cwd": str(cwd), "root": str(path), "basis": "git root"}
        if stop is not None and folded(path) == folded(stop):
            break
    for path in chain:
        tags = markers(path)
        if tags:
            return {"cwd": str(cwd), "root": str(path), "basis": "project marker", "markers": tags}
        if stop is not None and folded(path) == folded(stop):
            break
    return {"cwd": str(cwd), "root": None, "basis": None}


def read_center(db=None, config=None):
    """Locate the task centre. An explicit path wins; otherwise one config file decides.

    A missing database is reported, never created: the centre is a coordination artefact,
    and a session that invents one would silently split the record in two.
    """
    if db:
        path = Path(db).absolute()
        return {"available": path.is_file(), "path": str(path), "source": "argument",
                "reason": None if path.is_file() else "database missing at the given path"}
    config = Path(DEFAULT_CONFIG if config is None else config)
    if not config.is_file():
        return {"available": False, "path": None, "source": None, "config": str(config),
                "reason": "no centre configured"}
    try:
        declared = json.loads(config.read_text(encoding="utf-8")).get("db")
    except (OSError, ValueError):
        return {"available": False, "path": None, "source": str(config), "config": str(config),
                "reason": "configuration is unreadable"}
    if not isinstance(declared, str) or not declared.strip():
        return {"available": False, "path": None, "source": str(config), "config": str(config),
                "reason": "configuration has no db entry"}
    path = Path(declared).absolute()
    return {"available": path.is_file(), "path": str(path), "source": str(config), "config": str(config),
            "reason": None if path.is_file() else "database missing at the configured path"}


def look(start=None, roots=None, config=None, db=None, depth=DEFAULT_DEPTH,
         active_days=DEFAULT_ACTIVE_DAYS, limit=DEFAULT_LIMIT, jobs=DEFAULT_JOBS,
         coordinator=None, explain=False, stop=None):
    """Assemble what a session needs: where it is, where the centre is, what else exists."""
    centre = read_center(db, config)
    here = detect_here(start, stop)
    result = {"here": here, "center": centre}
    if here.get("root"):
        item = describe(Path(here["root"]), depth, active_days)
        result["here"].update({"name": item.get("name"), "activity": item.get("activity"),
                               "signals": item.get("signals", [])})
        registry = registered(centre["path"] if centre["available"] else None)
        # A git worktree lives inside its registered project, so its own .git root is deeper
        # than the registration. The deepest registered root that contains this location is
        # the project; both facts stay visible rather than one replacing the other.
        spot = folded(here["root"])
        containing = [row for row in registry["items"]
                      if spot[:len(folded(row["root"]))] == folded(row["root"])]
        match = max(containing, key=lambda row: len(folded(row["root"])), default=None)
        result["here"]["registered"] = ({"project_id": match["project_id"], "name": match["name"],
                                         "root": match["root"]}
                                        if match else None)
        result["registry"] = {"available": registry["available"], "reason": registry.get("reason")}
    if roots:
        scanned = collect(roots, db=centre["path"] if centre["available"] else None, depth=depth,
                          active_days=active_days, limit=limit, jobs=jobs,
                          coordinator=coordinator, explain=explain)
        result.update(scanned["result"])
        result.pop("registry", None)
    return {"ok": True, "result": result}


def brief(report):
    """Compact form for a session: what it costs to read, not everything there is to know."""
    body = report["result"]
    here = body["here"]
    # Names, locations and states only: enough to phrase one question. The evidence that
    # produced them stays in --full, where a human is asking instead of a model.
    near = [{"name": item.get("name"), "root": item.get("root"), "activity": item.get("activity"),
             "registered": item.get("project_id")}
            for item in (body.get("rank") or [])[:3]]
    out = {"here": {"cwd": here.get("cwd"), "root": here.get("root"), "basis": here.get("basis"),
                    "name": here.get("name"), "activity": here.get("activity"),
                    "registered": here.get("registered")},
           "center": {"available": body["center"]["available"], "path": body["center"]["path"],
                      "source": body["center"]["source"], "reason": body["center"].get("reason")},
           "near": near,
           "broken": [{"project_id": item.get("project_id"), "root": item.get("root")}
                      for item in (body.get("broken") or [])]}
    if body.get("counts"):
        out["counts"] = body["counts"]
    return {"ok": True, "result": out}


def collect(roots, db=None, depth=DEFAULT_DEPTH, active_days=DEFAULT_ACTIVE_DAYS, limit=DEFAULT_LIMIT,
            jobs=DEFAULT_JOBS, coordinator=None, explain=False):
    """Build the probe report. Read-only: no database write, no cache, no matching."""
    started = time.time()
    notes, items, seen = [], [], set()

    registry = registered(db)
    for row in registry["items"]:
        root = Path(row["root"])
        role = "coordinator" if row["project_id"] == coordinator else "member"
        if not root.is_dir():
            items.append({"project_id": row["project_id"], "name": row["name"], "root": row["root"],
                          "state": "broken", "role": role, "source": "registered",
                          "signals": ["registered root no longer exists"]})
            notes.append(f"registered {row['project_id']}: root missing")
            continue
        item = describe(root, depth, active_days)
        item.update({"project_id": row["project_id"], "name": row["name"], "state": "registered",
                     "role": role, "source": "registered"})
        items.append(item)
        seen.add(norm(root))

    scanned = 0
    pending = []
    for raw in roots:
        root = Path(raw)
        if not root.is_dir():
            notes.append(f"root not a directory: {raw}")
            continue
        for candidate in candidate_dirs(root, depth):
            if norm(candidate) in seen:
                continue
            seen.add(norm(candidate))
            pending.append(candidate)
            scanned += 1

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for item in pool.map(lambda path: describe(path, depth, active_days), pending):
            item["source"] = "scanned"
            items.append(item)

    broken = [item for item in items if item["state"] == "broken"]
    mark_nested(items)
    coordinator = next((item for item in items if item.get("role") == "coordinator"), None)
    ranked = order([item for item in items
                    if item["state"] != "broken" and item.get("role") != "coordinator"
                    and not item.get("nested")])
    omitted = ranked[limit:]
    result = {"ok": True, "result": {
        "roots": roots, "depth": depth, "active_days": active_days,
        "registry": {"available": registry["available"], "reason": registry.get("reason"),
                     "count": len(registry["items"])},
        "coordinator": coordinator, "rank": ranked[:limit], "broken": broken,
        "counts": {"ranked": len(ranked), "returned": min(len(ranked), limit), "broken": len(broken),
                   "scanned": scanned,
                   "nested": sum(1 for item in items if item.get("nested")),
                   "omitted": {band: sum(1 for item in omitted if item.get("activity") == band)
                               for band in BANDS}},
    }}
    if explain:
        result["result"]["explain"] = {"notes": notes, "elapsed_ms": int((time.time() - started) * 1000),
                                       "git_timeout_seconds": GIT_TIMEOUT, "file_cap": MAX_SCAN_FILES,
                                       "recent_window_days": RECENT_WINDOW_DAYS, "markers": list(MARKERS),
                                       "noise": sorted(NOISE)}
    return result


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Read-only project locator; decides nothing")
    parser.add_argument("--db", help="Centre database, opened read-only. Beats the config file")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="File holding {\"db\": <path>} (default %(default)s)")
    parser.add_argument("--here", dest="start", help="Directory to inspect instead of the current one")
    parser.add_argument("--roots", nargs="+", help="Extra parent directories to scan; omit to inspect nothing else")
    parser.add_argument("--depth", type=int, default=DEFAULT_DEPTH, help=f"Scan depth (default {DEFAULT_DEPTH})")
    parser.add_argument("--active-days", type=int, default=DEFAULT_ACTIVE_DAYS,
                        help=f"Activity window in days (default {DEFAULT_ACTIVE_DAYS})")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"Ranked items (default {DEFAULT_LIMIT})")
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS, help=f"Parallel git calls (default {DEFAULT_JOBS})")
    parser.add_argument("--coordinator", help="Registered project id hosting this conversation; listed, never ranked")
    parser.add_argument("--full", action="store_true", help="Print the whole report instead of the summary")
    parser.add_argument("--explain", action="store_true", help="Report what was scanned, skipped and why")
    try:
        args = parser.parse_args(argv)
    except ValueError:
        print(json.dumps({"ok": False, "error": {"code": "INVALID_INPUT", "message": "Invalid arguments"}}))
        return 2

    for name, value, low, high in (("depth", args.depth, 0, 4), ("limit", args.limit, 1, 100), ("jobs", args.jobs, 1, 32)):
        if not low <= value <= high:
            print(json.dumps({"ok": False, "error": {"code": "INVALID_INPUT",
                                                     "message": f"{name} must be {low}-{high}"}}))
            return 2
    for raw in args.roots or []:
        if not Path(raw).is_absolute():
            print(json.dumps({"ok": False, "error": {"code": "INVALID_INPUT", "message": "roots must be absolute"}}))
            return 2

    report = look(start=args.start, roots=args.roots, config=args.config, db=args.db, depth=args.depth,
                  active_days=args.active_days, limit=args.limit, jobs=args.jobs,
                  coordinator=args.coordinator, explain=args.explain)
    print(json.dumps(report if (args.full or args.explain) else brief(report),
                     ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
