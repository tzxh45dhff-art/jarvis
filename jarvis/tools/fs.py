"""Filesystem tools. Every mutation is journalled and undoable."""
from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
from pathlib import Path

from .. import context
from ..config import CONFIG
from ..safety import journal
from ..safety.policy import Risk, Verdict, classify_path_write
from .registry import REGISTRY, ToolResult

MAX_READ = 400_000

#: Dependency directories. Their contents are never what the user is asking
#: about, and they outnumber real matches by an order of magnitude.
_VENDOR = ("venv", ".venv", "node_modules", "site-packages", "__pycache__",
           ".git", "dist", "build", ".tox", ".mypy_cache")


def _p(path: str) -> Path:
    """
    Resolve what the model said into a real path.

    Relative names resolve against whatever the user has open on screen, not
    this process's working directory - the daemon starts in its own source tree,
    which is how "edit llm.py" once landed in jarvis/core/llm.py.
    """
    return context.resolve(str(path))


def _assess_write(args: dict) -> Verdict:
    return classify_path_write(args.get("path", ""))


def _assess_delete(args: dict) -> Verdict:
    paths = args.get("paths") or ([args["path"]] if args.get("path") else [])
    if len(paths) > CONFIG.safety.max_delete_batch:
        return Verdict(Risk.HIGH, f"deleting {len(paths)} items at once",
                       category="delete", needs_confirm=True)
    for p in paths:
        v = classify_path_write(p, deleting=True)
        if v.risk >= Risk.FORBIDDEN:
            return v
    return Verdict(Risk.HIGH, f"moves {len(paths)} item(s) to Trash",
                   category="delete", needs_confirm=True)


@REGISTRY.tool(
    "read_file",
    "Read a text file. Use offset/limit for large files. Returns the content with line numbers.",
    {"type": "object", "properties": {
        "path": {"type": "string", "description": "Path to the file"},
        "offset": {"type": "integer", "description": "First line to read (1-based)"},
        "limit": {"type": "integer", "description": "Max lines to read"}},
     "required": ["path"]},
    risk=Risk.SAFE, category="read")
def read_file(path: str, offset: int = 1, limit: int = 2000):
    f = _p(path)
    if not f.exists():
        return ToolResult(False, error=f"File not found: {f}")
    if f.is_dir():
        return ToolResult(False, error=f"{f} is a directory; use list_dir")
    if f.stat().st_size > MAX_READ:
        data = f.read_bytes()[:MAX_READ].decode("utf-8", "replace")
        note = f"\n... [truncated at {MAX_READ} bytes of {f.stat().st_size}]"
    else:
        try:
            data = f.read_text(errors="replace")
        except Exception as e:
            return ToolResult(False, error=f"Cannot read: {e}")
        note = ""
    lines = data.splitlines()
    sel = lines[max(0, offset - 1): max(0, offset - 1) + limit]
    body = "\n".join(f"{i + offset:>5}| {l}" for i, l in enumerate(sel))
    return ToolResult(True, content=body + note,
                      display=f"read {f.name} ({len(sel)} lines)",
                      meta={"path": str(f), "total_lines": len(lines)})


def _blocked_parent(f) -> str:
    """A clear reason a file cannot go at `f`, or "" (instead of a raw
    FileExistsError from mkdir when part of the path is a file)."""
    if f.is_dir():
        return f"{f} is a folder - give a file name inside it"
    for parent in f.parents:
        if parent.exists():
            return "" if parent.is_dir() else (
                f"{parent} is a file, not a folder, so nothing can be created "
                f"inside it - choose another path")
    return ""


@REGISTRY.tool(
    "write_file",
    "Write text to a file, creating parent directories. Overwrites are snapshotted so they can be undone.",
    {"type": "object", "properties": {
        "path": {"type": "string"},
        "content": {"type": "string", "description": "Full file content"}},
     "required": ["path", "content"]},
    risk=Risk.MEDIUM, category="write_new", assess=_assess_write, parallel_safe=False)
def write_file(path: str, content: str):
    f = _p(path)
    why = _blocked_parent(f)
    if why:
        return ToolResult(False, error=why)
    existed = f.exists()
    journal.record("overwrite" if existed else "create", str(f),
                   session=REGISTRY.session_id)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    # Read it back: this result is the check, so the model need not spend a
    # step (and a model call) looking at the file again.
    ok = f.exists() and f.read_text(errors="replace") == content
    return ToolResult(True,
                      content={"path": str(f), "bytes": len(content.encode()),
                               "replaced": existed, "verified": ok},
                      display=f"{'updated' if existed else 'created'} {f.name} "
                              f"({len(content.encode()):,} bytes)")


@REGISTRY.tool(
    "edit_file",
    "Replace an exact string in a file. The original is snapshotted first so the edit can be undone.",
    {"type": "object", "properties": {
        "path": {"type": "string"},
        "old": {"type": "string", "description": "Exact text to replace"},
        "new": {"type": "string", "description": "Replacement text"},
        "all": {"type": "boolean", "description": "Replace every occurrence"}},
     "required": ["path", "old", "new"]},
    risk=Risk.MEDIUM, category="overwrite", assess=_assess_write, parallel_safe=False)
def edit_file(path: str, old: str, new: str, all: bool = False):
    f = _p(path)
    if not f.exists():
        return ToolResult(False, error=f"File not found: {f}")
    text = f.read_text(errors="replace")
    n = text.count(old)
    if n == 0:
        return ToolResult(False, error="The 'old' string was not found. "
                                       "Read the file again and match exactly.")
    if n > 1 and not all:
        return ToolResult(False, error=f"'old' appears {n} times; pass all=true "
                                       f"or include more surrounding context.")
    journal.record("overwrite", str(f), session=REGISTRY.session_id)
    f.write_text(text.replace(old, new) if all else text.replace(old, new, 1))
    after = f.read_text(errors="replace")
    return ToolResult(True, content={"path": str(f), "replacements": n if all else 1,
                                     "verified": (new in after) if new else (after.count(old) < n)},
                      display=f"edited {f.name} ({n if all else 1} replacement)")


@REGISTRY.tool(
    "list_dir",
    "List a directory with sizes and types.",
    {"type": "object", "properties": {
        "path": {"type": "string"},
        "all": {"type": "boolean", "description": "Include dotfiles"}},
     "required": ["path"]},
    risk=Risk.SAFE, category="read")
def list_dir(path: str, all: bool = False):
    d = _p(path)
    if not d.exists():
        return ToolResult(False, error=f"Not found: {d}")
    if not d.is_dir():
        return ToolResult(False, error=f"{d} is not a directory")
    rows = []
    for e in sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if not all and e.name.startswith("."):
            continue
        try:
            st = e.stat()
            rows.append({"name": e.name + ("/" if e.is_dir() else ""),
                         "size": st.st_size if e.is_file() else None,
                         "modified": int(st.st_mtime)})
        except OSError:
            continue
    return ToolResult(True, content={"path": str(d), "entries": rows[:500],
                                     "count": len(rows)},
                      display=f"listed {d.name}/ ({len(rows)} entries)")


@REGISTRY.tool(
    "find_files",
    "Find files by name or glob, anywhere on the disk (Spotlight-backed). Leave path empty to search everywhere; set it to search one folder.",
    {"type": "object", "properties": {
        "pattern": {"type": "string", "description": "Glob, e.g. '*.py' or 'report*'"},
        "path": {"type": "string", "description": "Optional folder to limit the search to"},
        "limit": {"type": "integer"}},
     "required": ["pattern"]},
    risk=Risk.SAFE, category="search")
def find_files(pattern: str, path: str = "", limit: int = 100):
    """
    Spotlight first, directory walk second.

    Walking ~ took long enough to time out on a real home directory, so a file
    the user named but did not locate was simply never found. Spotlight has the
    whole volume indexed and answers in milliseconds; the walk stays as the
    fallback for un-indexed volumes and for patterns Spotlight cannot express.
    """
    root = _p(path) if path else context.workspace()
    hits: list[str] = []

    # A plain "*.ext" or "name*" pattern is something Spotlight can answer
    # directly, and it searches everywhere rather than under one root.
    if not path:
        for hit in context.locate(pattern, near=str(root), limit=limit):
            hits.append(str(hit))

    if not hits:
        skip = {"node_modules", ".git", "venv", ".venv", "__pycache__", "Library",
                ".cache", "site-packages", ".Trash", "dist", "build"}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in skip and not d.startswith(".")]
            for fn in filenames:
                if fnmatch.fnmatch(fn, pattern):
                    hits.append(os.path.join(dirpath, fn))
                    if len(hits) >= limit:
                        return ToolResult(
                            True,
                            content={"matches": hits, "truncated": True,
                                     "searched": str(root)},
                            display=f"found {len(hits)}+ files")

    return ToolResult(True,
                      content={"matches": hits, "count": len(hits),
                               "searched": str(root) if path else "everywhere"},
                      display=f"found {len(hits)} file(s)")


@REGISTRY.tool(
    "search_text",
    "Search file contents with ripgrep-style matching. Returns file:line:text.",
    {"type": "object", "properties": {
        "query": {"type": "string"},
        "path": {"type": "string"},
        "glob": {"type": "string", "description": "Restrict to files matching this glob"},
        "limit": {"type": "integer"}},
     "required": ["query"]},
    risk=Risk.SAFE, category="search")
def search_text(query: str, path: str = "", glob: str = "", limit: int = 60):
    root = str(_p(path) if path else context.workspace())
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "--line-number", "--no-heading", "--color", "never",
               "--max-count", "5", "-S", query, root]
        # Vendored code buries the user's own matches: a two-line project
        # returned 29 hits, 27 of them from site-packages.
        for d in _VENDOR:
            cmd[1:1] = ["--glob", f"!**/{d}/**"]
        if glob:
            cmd[1:1] = ["--glob", glob]
    else:
        # The grep fallback is the path that actually runs on a machine without
        # ripgrep, so it needs the same exclusions - without them a two-file
        # project returned 29 hits, 25 of them from site-packages.
        cmd = ["grep", "-rn", "--binary-files=without-match"]
        for d in _VENDOR:
            cmd.append(f"--exclude-dir={d}")
        if glob:
            cmd.append(f"--include={glob}")
        cmd += [query, root]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        return ToolResult(False, error="search timed out")
    lines = [l for l in r.stdout.splitlines() if l][:limit]
    return ToolResult(True, content={"matches": lines, "count": len(lines)},
                      display=f"{len(lines)} match(es) for {query!r}")


def _dest_path(s, dest: str):
    """Where `s` really goes, and a note when `dest` was read as a folder.

    A destination that does not exist yet, ends in '/', or has no extension
    while the source file has one is a folder: move_file(main.java,
    src/com/example) used to rename the file to an extensionless 'example',
    breaking the project and every later write into src/com/example/."""
    d = _p(dest)
    if d.is_dir():
        return d / s.name, ""
    # (Same folder = a rename: README.md -> README stays a rename.)
    if not d.exists() and (dest.rstrip().endswith(("/", os.sep)) or
                           (s.is_file() and s.suffix and not d.suffix
                            and d.parent != s.parent)):
        return d / s.name, (f"'{d.name}' was taken as a folder (it did not exist "
                            f"and has no extension); the file keeps its name")
    return d, ""


@REGISTRY.tool(
    "move_file",
    "Move or rename a file or folder. Reversible.",
    {"type": "object", "properties": {
        "source": {"type": "string"}, "dest": {"type": "string"}},
     "required": ["source", "dest"]},
    risk=Risk.MEDIUM, category="overwrite", parallel_safe=False,
    assess=lambda a: classify_path_write(a.get("dest", "")))
def move_file(source: str, dest: str):
    s = _p(source)
    if not s.exists():
        return ToolResult(False, error=f"Source not found: {s}")
    d, note = _dest_path(s, dest)
    why = _blocked_parent(d) if not d.exists() else ""
    if why:
        return ToolResult(False, error=why)
    if d.exists():
        journal.record("overwrite", str(d), session=REGISTRY.session_id)
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(s), str(d))
    journal.record("move", str(s), dest=str(d), snapshot=False,
                   session=REGISTRY.session_id)
    out = {"from": str(s), "to": str(d), "verified": d.exists() and not s.exists()}
    if note:
        out["note"] = note
    return ToolResult(True, content=out, display=f"moved {s.name} -> {d}")


@REGISTRY.tool(
    "copy_file",
    "Copy a file or folder.",
    {"type": "object", "properties": {
        "source": {"type": "string"}, "dest": {"type": "string"}},
     "required": ["source", "dest"]},
    risk=Risk.LOW, category="write_new", parallel_safe=False)
def copy_file(source: str, dest: str):
    s = _p(source)
    if not s.exists():
        return ToolResult(False, error=f"Source not found: {s}")
    d, note = _dest_path(s, dest)
    why = _blocked_parent(d) if not d.exists() else ""
    if why:
        return ToolResult(False, error=why)
    d.parent.mkdir(parents=True, exist_ok=True)
    if s.is_dir():
        shutil.copytree(s, d, dirs_exist_ok=True)
    else:
        shutil.copy2(s, d)
    journal.record("create", str(d), snapshot=False, session=REGISTRY.session_id)
    out = {"from": str(s), "to": str(d), "verified": d.exists()}
    if note:
        out["note"] = note
    return ToolResult(True, content=out, display=f"copied {s.name} -> {d}")


@REGISTRY.tool(
    "trash_files",
    "Move files or folders to the macOS Trash. Never permanently deletes; "
    "everything stays recoverable from Finder.",
    {"type": "object", "properties": {
        "paths": {"type": "array", "items": {"type": "string"},
                  "description": "Paths to move to Trash"}},
     "required": ["paths"]},
    risk=Risk.HIGH, category="delete", assess=_assess_delete, parallel_safe=False)
def trash_files(paths: list):
    if isinstance(paths, str):
        paths = [paths]
    done, failed = [], []
    for p in paths:
        journal.record("delete", str(_p(p)), session=REGISTRY.session_id)
        ok, msg = journal.trash(str(_p(p)))
        (done if ok else failed).append(msg)
    return ToolResult(bool(done) or not failed,
                      content={"trashed": done, "failed": failed},
                      display=f"moved {len(done)} item(s) to Trash")


@REGISTRY.tool(
    "make_dir",
    "Create a directory (including parents).",
    {"type": "object", "properties": {"path": {"type": "string"}},
     "required": ["path"]},
    risk=Risk.LOW, category="write_new")
def make_dir(path: str):
    d = _p(path)
    d.mkdir(parents=True, exist_ok=True)
    return ToolResult(True, content={"path": str(d), "verified": d.is_dir()},
                      display=f"created {d}/")


@REGISTRY.tool(
    "file_info",
    "Stat a path: existence, size, type, modified time.",
    {"type": "object", "properties": {"path": {"type": "string"}},
     "required": ["path"]},
    risk=Risk.SAFE, category="read")
def file_info(path: str):
    f = _p(path)
    if not f.exists():
        return ToolResult(True, content={"exists": False, "path": str(f)},
                          display=f"{f.name}: does not exist")
    st = f.stat()
    return ToolResult(True, content={
        "exists": True, "path": str(f), "is_dir": f.is_dir(),
        "size": st.st_size, "modified": int(st.st_mtime)},
        display=f"{f.name}: {st.st_size:,} bytes")
