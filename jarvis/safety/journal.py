"""
Mutation journal + undo.

Every filesystem change JARVIS makes is recorded here *before* it happens, with
enough information to reverse it:

  * overwrite / edit -> the original bytes are snapshotted to the backup store
  * delete           -> the item is moved to the macOS Trash (Finder "Put Back"
                        still works), never unlinked
  * create           -> reversed by trashing the created file
  * move / rename    -> reversed by moving back

This is what lets the assistant act autonomously without risking your data.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..config import DATA_DIR

BACKUP_DIR = DATA_DIR / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
JOURNAL_DB = DATA_DIR / "journal.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mutations (
    id          TEXT PRIMARY KEY,
    ts          REAL NOT NULL,
    session     TEXT,
    op          TEXT NOT NULL,          -- create|overwrite|delete|move
    path        TEXT NOT NULL,
    dest        TEXT,
    backup      TEXT,
    meta        TEXT,
    undone      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mut_ts ON mutations(ts DESC);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(JOURNAL_DB, timeout=10)
    c.executescript(_SCHEMA)
    return c


@dataclass
class Mutation:
    id: str
    ts: float
    op: str
    path: str
    dest: str | None = None
    backup: str | None = None
    meta: dict | None = None
    undone: bool = False


def _snapshot(path: Path) -> str | None:
    """Copy a file's current contents into the backup store."""
    if not path.exists() or path.is_dir():
        return None
    h = hashlib.sha256(f"{path}{time.time()}{uuid.uuid4()}".encode()).hexdigest()[:20]
    dest = BACKUP_DIR / f"{h}{path.suffix}"
    try:
        shutil.copy2(path, dest)
        return str(dest)
    except Exception:
        return None


def record(op: str, path: str, dest: str | None = None,
           snapshot: bool = True, session: str = "", meta: dict | None = None) -> str:
    """Record a mutation about to happen. Returns the mutation id."""
    p = Path(os.path.expanduser(path))
    backup = _snapshot(p) if (snapshot and op in ("overwrite", "delete")) else None
    mid = uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute(
            "INSERT INTO mutations(id,ts,session,op,path,dest,backup,meta) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (mid, time.time(), session, op, str(p), dest, backup,
             json.dumps(meta or {})),
        )
    return mid


def trash(path: str) -> tuple[bool, str]:
    """
    Move to the macOS Trash instead of deleting. Recoverable from Finder.
    Falls back to a manual move into ~/.Trash if the Cocoa API is unavailable.
    """
    p = Path(os.path.expanduser(path))
    if not p.exists():
        return False, f"not found: {p}"
    try:
        from Foundation import NSURL, NSFileManager
        url = NSURL.fileURLWithPath_(str(p))
        ok, _new, err = NSFileManager.defaultManager().\
            trashItemAtURL_resultingItemURL_error_(url, None, None)
        if ok:
            return True, f"moved to Trash: {p.name}"
        raise RuntimeError(str(err))
    except Exception:
        try:
            tdir = Path.home() / ".Trash"
            tdir.mkdir(exist_ok=True)
            target = tdir / p.name
            n = 1
            while target.exists():
                target = tdir / f"{p.stem} {n}{p.suffix}"
                n += 1
            shutil.move(str(p), str(target))
            return True, f"moved to Trash: {target.name}"
        except Exception as e:  # pragma: no cover
            return False, f"could not trash {p}: {e}"


def recent(limit: int = 20, include_undone: bool = False) -> list[Mutation]:
    q = "SELECT id,ts,op,path,dest,backup,meta,undone FROM mutations"
    if not include_undone:
        q += " WHERE undone=0"
    q += " ORDER BY ts DESC LIMIT ?"
    with _conn() as c:
        rows = c.execute(q, (limit,)).fetchall()
    return [
        Mutation(id=r[0], ts=r[1], op=r[2], path=r[3], dest=r[4], backup=r[5],
                 meta=json.loads(r[6] or "{}"), undone=bool(r[7]))
        for r in rows
    ]


def undo(mutation_id: str | None = None) -> tuple[bool, str]:
    """Reverse the most recent mutation, or a specific one by id."""
    with _conn() as c:
        if mutation_id:
            row = c.execute(
                "SELECT id,op,path,dest,backup FROM mutations WHERE id=? AND undone=0",
                (mutation_id,)).fetchone()
        else:
            row = c.execute(
                "SELECT id,op,path,dest,backup FROM mutations WHERE undone=0 "
                "ORDER BY ts DESC LIMIT 1").fetchone()
        if not row:
            return False, "nothing to undo"
        mid, op, path, dest, backup = row
        p = Path(path)
        try:
            if op in ("overwrite", "delete"):
                if not backup or not Path(backup).exists():
                    return False, f"no snapshot stored for {p.name}; cannot undo"
                p.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, p)
                msg = f"restored {p}"
            elif op == "create":
                ok, msg = trash(str(p))
                if not ok:
                    return False, msg
            elif op == "move":
                if dest and Path(dest).exists():
                    shutil.move(dest, str(p))
                    msg = f"moved back to {p}"
                else:
                    return False, "destination missing; cannot undo move"
            else:
                return False, f"cannot undo op '{op}'"
        except Exception as e:
            return False, f"undo failed: {e}"
        c.execute("UPDATE mutations SET undone=1 WHERE id=?", (mid,))
    return True, msg


def undo_session(session: str) -> list[str]:
    """Reverse every mutation from one session, newest first."""
    with _conn() as c:
        ids = [r[0] for r in c.execute(
            "SELECT id FROM mutations WHERE session=? AND undone=0 ORDER BY ts DESC",
            (session,)).fetchall()]
    out = []
    for i in ids:
        ok, msg = undo(i)
        out.append(("OK  " if ok else "ERR ") + msg)
    return out
