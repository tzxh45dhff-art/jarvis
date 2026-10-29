"""
Persistent memory.

Four kinds, per the design brief:

  working   - the live conversation (held in the session, not here)
  episodic  - what happened: past interactions, tasks, outcomes
  semantic  - stable facts: preferences, identity, habits
  project   - ongoing work: goals, decisions, open threads

Storage is SQLite (source of truth) with 384-d embeddings stored alongside each
row. Retrieval is exact cosine similarity in NumPy: at personal-assistant scale
(tens of thousands of rows) that is sub-millisecond and removes a whole class of
index-drift bugs a separate vector store would introduce.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np

from ..config import CONFIG

KINDS = ("semantic", "episodic", "project", "preference")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    content    TEXT NOT NULL,
    subject    TEXT,
    tags       TEXT,
    source     TEXT,
    created    REAL NOT NULL,
    updated    REAL NOT NULL,
    accessed   REAL,
    hits       INTEGER DEFAULT 0,
    confidence REAL DEFAULT 1.0,
    active     INTEGER DEFAULT 1,
    embedding  BLOB
);
CREATE INDEX IF NOT EXISTS idx_kind   ON memories(kind, active);
CREATE INDEX IF NOT EXISTS idx_update ON memories(updated DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
    USING fts5(id UNINDEXED, content, subject, tokenize='porter');
"""

# Patterns that must never be captured automatically.
_SENSITIVE = [
    re.compile(p, re.I) for p in (
        r"\bsk-[A-Za-z0-9_\-]{16,}",           # API keys
        r"\bghp_[A-Za-z0-9]{20,}",
        r"AKIA[0-9A-Z]{16}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\b(?:\d[ -]*?){13,16}\b",            # card numbers
        r"\b\d{3}-\d{2}-\d{4}\b",              # SSN
        r"\b(password|passwd|api[_ ]?key|secret|token|credential)\b\s*[:=]\s*\S+",
    )
]


def looks_sensitive(text: str) -> bool:
    return any(p.search(text or "") for p in _SENSITIVE)


@dataclass
class Memory:
    id: str
    kind: str
    content: str
    subject: str = ""
    tags: list[str] = field(default_factory=list)
    source: str = ""
    created: float = 0.0
    updated: float = 0.0
    hits: int = 0
    confidence: float = 1.0
    score: float = 0.0

    def line(self) -> str:
        age = time.time() - (self.updated or self.created)
        when = ("today" if age < 86400 else
                f"{int(age // 86400)}d ago" if age < 86400 * 30 else
                time.strftime("%Y-%m-%d", time.localtime(self.updated or self.created)))
        subj = f"[{self.subject}] " if self.subject else ""
        return f"({self.kind}, {when}) {subj}{self.content}"


class Embedder:
    """Lazy sentence-transformers wrapper; the model is already on disk."""

    def __init__(self, name: str | None = None):
        self.name = name or CONFIG.memory.embed_model
        self._m = None
        self._lock = threading.Lock()

    def load(self):
        with self._lock:
            if self._m is None:
                from sentence_transformers import SentenceTransformer
                self._m = SentenceTransformer(self.name, device="mps")
            return self._m

    def encode(self, texts: list[str]) -> np.ndarray:
        m = self.load()
        v = m.encode(texts, normalize_embeddings=True,
                     convert_to_numpy=True, show_progress_bar=False)
        return v.astype(np.float32)


class MemoryStore:
    def __init__(self, path=None, embedder: Embedder | None = None):
        self.path = str(path or CONFIG.memory.db_path)
        self.embedder = embedder or Embedder()
        self._lock = threading.RLock()
        with self._conn() as c:
            c.executescript(_SCHEMA)
        self._cache_vecs: np.ndarray | None = None
        self._cache_ids: list[str] = []
        self._dirty = True

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    # -- writing -----------------------------------------------------------
    def add(self, content: str, kind: str = "semantic", subject: str = "",
            tags: Iterable[str] = (), source: str = "", confidence: float = 1.0,
            allow_sensitive: bool = False) -> tuple[str | None, str]:
        content = (content or "").strip()
        if not content:
            return None, "empty memory"
        if not allow_sensitive and looks_sensitive(content):
            return None, ("refused: this looks like a credential or other sensitive "
                          "value; not stored")
        if kind not in KINDS:
            kind = "semantic"
        # Update instead of duplicating a near-identical memory.
        dupe = self._find_duplicate(content, kind)
        if dupe:
            self.update(dupe.id, content=content, confidence=confidence)
            return dupe.id, "updated an existing, closely-related memory"
        mid = uuid.uuid4().hex[:12]
        vec = self.embedder.encode([content])[0]
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO memories(id,kind,content,subject,tags,source,created,"
                "updated,accessed,hits,confidence,active,embedding) "
                "VALUES(?,?,?,?,?,?,?,?,?,0,?,1,?)",
                (mid, kind, content, subject, json.dumps(list(tags)), source,
                 now, now, now, confidence, vec.tobytes()))
            c.execute("INSERT INTO memories_fts(id,content,subject) VALUES(?,?,?)",
                      (mid, content, subject))
        self._dirty = True
        return mid, "stored"

    def _find_duplicate(self, content: str, kind: str, thresh: float = 0.88):
        # Compare across every kind: the same fact volunteered in conversation and
        # stored explicitly would otherwise land twice under different kinds.
        hits = self.search(content, k=3, min_score=0.5)
        for h in hits:
            a = self.embedder.encode([content])[0]
            b = self.embedder.encode([h.content])[0]
            if float(a @ b) >= thresh:
                return h
        return None

    def update(self, mid: str, content: str | None = None,
               confidence: float | None = None, subject: str | None = None) -> bool:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT content FROM memories WHERE id=?", (mid,)).fetchone()
            if not row:
                return False
            new = content if content is not None else row[0]
            vec = self.embedder.encode([new])[0]
            c.execute("UPDATE memories SET content=?, updated=?, embedding=?, "
                      "confidence=COALESCE(?,confidence), subject=COALESCE(?,subject) "
                      "WHERE id=?",
                      (new, time.time(), vec.tobytes(), confidence, subject, mid))
            c.execute("UPDATE memories_fts SET content=? WHERE id=?", (new, mid))
        self._dirty = True
        return True

    def forget(self, mid: str, hard: bool = False) -> bool:
        with self._lock, self._conn() as c:
            if hard:
                c.execute("DELETE FROM memories WHERE id=?", (mid,))
                c.execute("DELETE FROM memories_fts WHERE id=?", (mid,))
            else:
                n = c.execute("UPDATE memories SET active=0 WHERE id=?", (mid,)).rowcount
                if not n:
                    return False
        self._dirty = True
        return True

    def clear(self, kind: str | None = None) -> int:
        with self._lock, self._conn() as c:
            if kind:
                n = c.execute("UPDATE memories SET active=0 WHERE kind=?", (kind,)).rowcount
            else:
                n = c.execute("UPDATE memories SET active=0").rowcount
        self._dirty = True
        return n

    # -- reading -----------------------------------------------------------
    def _matrix(self) -> tuple[np.ndarray, list[str]]:
        if not self._dirty and self._cache_vecs is not None:
            return self._cache_vecs, self._cache_ids
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, embedding FROM memories WHERE active=1").fetchall()
        if not rows:
            self._cache_vecs = np.zeros((0, CONFIG.memory.embed_dim), dtype=np.float32)
            self._cache_ids = []
        else:
            self._cache_ids = [r[0] for r in rows]
            self._cache_vecs = np.stack(
                [np.frombuffer(r[1], dtype=np.float32) for r in rows])
        self._dirty = False
        return self._cache_vecs, self._cache_ids

    @staticmethod
    def _fts_query(q: str) -> str:
        """Turn free text into a safe FTS5 OR-query."""
        toks = re.findall(r"[A-Za-z0-9_]+", q.lower())
        stop = {"what", "who", "when", "where", "why", "how", "the", "a", "an",
                "is", "are", "was", "were", "do", "does", "did", "i", "me", "my",
                "you", "your", "it", "to", "of", "in", "on", "for", "and", "or",
                "should", "would", "can", "about", "that", "this", "with", "at",
                "right", "now", "tell", "remember"}
        toks = [t for t in toks if t not in stop and len(t) > 2][:12]
        return " OR ".join(toks) if toks else ""

    def _lexical(self, query: str, limit: int = 20) -> list[tuple[str, float]]:
        fq = self._fts_query(query)
        if not fq:
            return []
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT f.id, bm25(memories_fts) AS r FROM memories_fts f "
                    "JOIN memories m ON m.id = f.id "
                    "WHERE memories_fts MATCH ? AND m.active=1 "
                    "ORDER BY r LIMIT ?", (fq, limit)).fetchall()
            return [(r[0], float(r[1])) for r in rows]
        except sqlite3.OperationalError:
            return []

    def search(self, query: str, k: int | None = None,
               kinds: list[str] | None = None,
               min_score: float | None = None) -> list[Memory]:
        """
        Hybrid retrieval: dense cosine + BM25, fused with reciprocal rank fusion.

        Dense alone is noisy at this scale - a distractor can outrank the true
        match by a few hundredths. Lexical evidence breaks those ties, and RRF
        needs no score calibration between the two rankers.
        """
        k = k or CONFIG.memory.top_k
        min_score = CONFIG.memory.min_score if min_score is None else min_score
        mat, ids = self._matrix()
        if len(ids) == 0:
            return []
        qv = self.embedder.encode([query])[0]
        sims = mat @ qv
        dense_order = np.argsort(-sims)[: max(k * 5, 25)]
        dense_ranked = [(ids[i], float(sims[i])) for i in dense_order]
        dense_score = {i: s for i, s in dense_ranked}

        # Anything below the floor is treated as noise and cannot be surfaced.
        eligible = {i for i, s in dense_ranked if s >= min_score}
        lex = self._lexical(query, limit=max(k * 4, 20))
        for mid, _ in lex:
            # lexical hits still need a minimum of semantic plausibility
            if dense_score.get(mid, -1.0) >= min_score * 0.5:
                eligible.add(mid)
        if not eligible:
            return []

        K = 60.0
        fused: dict[str, float] = {}
        for rank, (mid, _) in enumerate(dense_ranked):
            if mid in eligible:
                fused[mid] = fused.get(mid, 0.0) + 1.0 / (K + rank + 1)
        for rank, (mid, _) in enumerate(lex):
            if mid in eligible:
                fused[mid] = fused.get(mid, 0.0) + 1.0 / (K + rank + 1)

        qmarks = ",".join("?" * len(fused))
        with self._conn() as c:
            rows = c.execute(
                f"SELECT id,kind,content,subject,tags,source,created,updated,hits,"
                f"confidence FROM memories WHERE id IN ({qmarks}) AND active=1",
                list(fused)).fetchall()
        out = []
        for r in rows:
            if kinds and r[1] not in kinds:
                continue
            out.append(Memory(
                id=r[0], kind=r[1], content=r[2], subject=r[3] or "",
                tags=json.loads(r[4] or "[]"), source=r[5] or "",
                created=r[6], updated=r[7], hits=r[8], confidence=r[9],
                score=dense_score.get(r[0], 0.0)))
        now = time.time()
        out.sort(key=lambda m: fused.get(m.id, 0.0)
                 + 0.0008 * float(np.exp(-(now - m.updated) / (86400 * 21))),
                 reverse=True)
        top = out[:k]
        self._touch([m.id for m in top])
        return top

    def _touch(self, ids: list[str]) -> None:
        if not ids:
            return
        with self._lock, self._conn() as c:
            c.executemany("UPDATE memories SET hits=hits+1, accessed=? WHERE id=?",
                          [(time.time(), i) for i in ids])

    def all(self, kind: str | None = None, limit: int = 200) -> list[Memory]:
        q = ("SELECT id,kind,content,subject,tags,source,created,updated,hits,"
             "confidence FROM memories WHERE active=1")
        args: list[Any] = []
        if kind:
            q += " AND kind=?"
            args.append(kind)
        q += " ORDER BY updated DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            rows = c.execute(q, args).fetchall()
        return [Memory(id=r[0], kind=r[1], content=r[2], subject=r[3] or "",
                       tags=json.loads(r[4] or "[]"), source=r[5] or "",
                       created=r[6], updated=r[7], hits=r[8], confidence=r[9])
                for r in rows]

    def count(self) -> dict[str, int]:
        with self._conn() as c:
            rows = c.execute("SELECT kind, COUNT(*) FROM memories WHERE active=1 "
                             "GROUP BY kind").fetchall()
        return {r[0]: r[1] for r in rows}


_STORE: MemoryStore | None = None


def get_store() -> MemoryStore:
    global _STORE
    if _STORE is None:
        _STORE = MemoryStore()
    return _STORE
