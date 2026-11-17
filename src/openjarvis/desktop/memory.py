"""Long-term memory for the desktop agent (SQLite + FTS5).

Three kinds of entries:

- ``fact``: things the user asked Jarvis to remember ("my name is Saketh",
  "I like lo-fi music").
- ``place``: learned locations, e.g. "minecraft server" -> a folder that
  had to be searched for once; next time "open the Minecraft server
  folder" resolves instantly.
- ``episode``: a short log of finished requests ("opened Netflix"), for
  "what did I ask you earlier?".

Relevant facts and places are recalled into each request automatically.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

_STOP_WORDS = frozenset(
    """a an and are as at be but by can could do does for from have hey how i
    in is it jarvis me my of on or please so that the this to up was we what
    when where which who will with would you your open go show tell set make
    find get turn start play close now just""".split()
)
_MAX_EPISODES = 500


@dataclass(slots=True)
class Memory:
    id: int
    kind: str
    subject: str
    content: str
    created: float

    def describe(self) -> str:
        if self.kind == "place":
            return f"[{self.id}] {self.subject} is at {self.content}"
        if self.kind == "episode":
            when = time.strftime("%a %d %b %H:%M", time.localtime(self.created))
            return f"[{self.id}] {when}: {self.content}"
        return f"[{self.id}] {self.content}"


def default_memory_path() -> Path:
    from openjarvis.core.paths import get_config_dir

    return get_config_dir() / "desktop" / "memory.sqlite"


def _terms(text: str) -> List[str]:
    words = re.findall(r"[\w']+", text.lower())
    return [w for w in words if len(w) > 1 and w not in _STOP_WORDS][:12]


class MemoryStore:
    """Thread-safe SQLite store; one file, safe across processes."""

    def __init__(self, path: Optional[Path | str] = None) -> None:
        self.path = Path(path) if path is not None else default_memory_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                subject TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                created REAL NOT NULL,
                used REAL NOT NULL DEFAULT 0
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                subject, content, content='memories', content_rowid='id',
                tokenize='porter unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, subject, content)
                VALUES (new.id, new.subject, new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, subject, content)
                VALUES ('delete', old.id, old.subject, old.content);
            END;
            CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, subject, content)
                VALUES ('delete', old.id, old.subject, old.content);
                INSERT INTO memories_fts(rowid, subject, content)
                VALUES (new.id, new.subject, new.content);
            END;
            """
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writing ------------------------------------------------------------

    def remember(self, content: str, *, kind: str = "fact", subject: str = "") -> int:
        content = " ".join(content.split())[:1000]
        subject = " ".join(subject.lower().split())[:200]
        if not content:
            raise ValueError("Nothing to remember.")
        with self._lock:
            if kind == "place" and subject:
                row = self._conn.execute(
                    "SELECT id FROM memories WHERE kind='place' AND subject=?",
                    (subject,),
                ).fetchone()
                if row:
                    self._conn.execute(
                        "UPDATE memories SET content=?, created=? WHERE id=?",
                        (content, time.time(), row[0]),
                    )
                    self._conn.commit()
                    return int(row[0])
            elif kind == "fact":
                row = self._conn.execute(
                    "SELECT id FROM memories WHERE kind='fact' AND content=?",
                    (content,),
                ).fetchone()
                if row:
                    return int(row[0])
            cur = self._conn.execute(
                "INSERT INTO memories(kind, subject, content, created) "
                "VALUES (?, ?, ?, ?)",
                (kind, subject, content, time.time()),
            )
            if kind == "episode":
                self._conn.execute(
                    "DELETE FROM memories WHERE kind='episode' AND id NOT IN "
                    "(SELECT id FROM memories WHERE kind='episode' "
                    "ORDER BY id DESC LIMIT ?)",
                    (_MAX_EPISODES,),
                )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def remember_place(self, name: str, location: str) -> int:
        return self.remember(location, kind="place", subject=name)

    def forget(self, memory_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def forget_matching(self, text: str) -> List[Memory]:
        """Forget facts/places best matching ``text``; returns what went."""
        hits = [m for m in self.search(text, limit=3) if m.kind != "episode"]
        if not hits:
            return []
        best = hits[:1]
        for memory in best:
            self.forget(memory.id)
        return best

    def clear(self, kind: Optional[str] = None) -> int:
        with self._lock:
            if kind:
                cur = self._conn.execute("DELETE FROM memories WHERE kind=?", (kind,))
            else:
                cur = self._conn.execute("DELETE FROM memories")
            self._conn.commit()
            return cur.rowcount

    # -- reading ------------------------------------------------------------

    def _rows(self, sql: str, args: tuple) -> List[Memory]:
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [Memory(int(r[0]), r[1], r[2], r[3], float(r[4])) for r in rows]

    def search(
        self, text: str, *, kinds: Optional[List[str]] = None, limit: int = 5
    ) -> List[Memory]:
        terms = _terms(text)
        if not terms:
            return []
        query = " OR ".join(f'"{t}"*' for t in terms)
        sql = (
            "SELECT m.id, m.kind, m.subject, m.content, m.created "
            "FROM memories_fts f JOIN memories m ON m.id = f.rowid "
            "WHERE memories_fts MATCH ?"
        )
        args: list = [query]
        if kinds:
            sql += f" AND m.kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        sql += " ORDER BY bm25(memories_fts, 4.0, 1.0) LIMIT ?"
        args.append(limit)
        try:
            return self._rows(sql, tuple(args))
        except sqlite3.OperationalError:
            return []

    def recall_place(self, name: str) -> Optional[str]:
        """Location remembered for ``name`` (exact, then close match)."""
        name = " ".join(name.lower().split())
        rows = self._rows(
            "SELECT id, kind, subject, content, created FROM memories "
            "WHERE kind='place' AND subject=?",
            (name,),
        )
        if rows:
            return rows[0].content
        words = set(_terms(name))
        for hit in self.search(name, kinds=["place"], limit=3):
            # Every spoken word must be part of the remembered name, so
            # "minecraft" finds "minecraft server" but "server" alone doesn't
            # pick an arbitrary place that happens to contain it... unless
            # it's the only word the place is known by.
            if words and words <= set(_terms(hit.subject)):
                return hit.content
        return None

    def recent(self, kind: Optional[str] = None, limit: int = 10) -> List[Memory]:
        if kind:
            return self._rows(
                "SELECT id, kind, subject, content, created FROM memories "
                "WHERE kind=? ORDER BY id DESC LIMIT ?",
                (kind, limit),
            )
        return self._rows(
            "SELECT id, kind, subject, content, created FROM memories "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def all(self, kind: Optional[str] = None) -> List[Memory]:
        return self.recent(kind, limit=100000)

    def context_for(self, request: str, limit: int = 4) -> str:
        """Memory lines worth showing the model for this request."""
        hits = self.search(request, kinds=["fact", "place"], limit=limit)
        return "\n".join(f"- {m.describe().split('] ', 1)[-1]}" for m in hits)


__all__ = ["Memory", "MemoryStore", "default_memory_path"]
