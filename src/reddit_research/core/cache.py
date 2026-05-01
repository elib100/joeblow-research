"""SQLite-backed cache for Reddit ``.json`` responses.

The cache is keyed by ``(kind, cache_key)`` where ``kind`` is the operation
type (``search`` / ``listing`` / ``thread`` / ``comment_subtree``) and
``cache_key`` is the fully-normalized key from :mod:`reddit_research.core.keys`
(includes all retrieval parameters + a ``:v1`` normalization suffix).

Both successful (HTTP 200) and permanent-error (HTTP 403 / 404) responses
share the same row layout — :attr:`CacheHit.status_code` discriminates. This
is a deliberate simplification from the manifest's earlier "separate
``error_permanent`` kind" design: a cached 404 for a thread lives 24h (the
thread TTL) which is the same TTL the design table called for, and operations
only need to do one lookup per request rather than two. Transient (5xx /
network) errors are never cached — they propagate to the caller untouched.

WAL mode + ``busy_timeout=5000`` + ``synchronous=NORMAL`` are set per
``CLAUDE.md`` decision 4, supporting CLI ↔ MCP-server contention on the same
file. Single-threaded within a process; multiple processes coexist via SQLite
WAL.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Per-kind TTLs in seconds. From CLAUDE.md `## Cache design`.
TTL_BY_KIND: dict[str, int] = {
    "search": 15 * 60,
    "listing": 60 * 60,
    "thread": 24 * 60 * 60,
    "comment_subtree": 24 * 60 * 60,
}

VALID_KINDS = frozenset(TTL_BY_KIND.keys())

# Only permanent error responses are cacheable. Transient / network errors
# bubble up — caching them would poison the entry on a single Reddit blip.
CACHEABLE_ERROR_STATUS_CODES = frozenset({403, 404})

SCHEMA_VERSION = 1


# ---- Result types ---------------------------------------------------------


@dataclass(frozen=True)
class CacheHit:
    """A non-stale cached entry. ``status_code`` is 200 for successful
    responses; 403/404 for cached permanent errors."""

    body: Any  # parsed JSON dict (or list for thread+comments responses)
    status_code: int
    fetched_at: int
    age_seconds: int


@dataclass
class CacheStats:
    """In-memory hit/miss counters + on-disk size snapshot.

    Counters are per-:class:`Cache` instance; not persisted across processes.
    """

    hits: int = 0
    misses: int = 0
    writes: int = 0
    db_size_bytes: int = 0
    row_count: int = 0

    @property
    def hit_rate(self) -> float | None:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else None


# ---- Cache ----------------------------------------------------------------


class Cache:
    """SQLite-backed cache. Open lazily on first use; close via context manager
    or :meth:`close`.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._stats_hits = 0
        self._stats_misses = 0
        self._stats_writes = 0

    # ---- public ----

    def get(self, kind: str, key: str, *, now: int | None = None) -> CacheHit | None:
        """Look up an entry. Returns None on miss or staleness.

        Hits and misses both increment the in-memory counters; staleness
        counts as a miss.
        """
        self._validate_kind(kind)
        conn = self._connect()
        row = conn.execute(
            "SELECT fetched_at, status_code, body_json "
            "FROM cached_objects WHERE kind = ? AND cache_key = ?",
            (kind, key),
        ).fetchone()
        if row is None:
            self._stats_misses += 1
            return None
        now_ts = int(time.time()) if now is None else int(now)
        age = now_ts - int(row["fetched_at"])
        if age >= TTL_BY_KIND[kind]:
            self._stats_misses += 1
            return None
        self._stats_hits += 1
        return CacheHit(
            body=json.loads(row["body_json"]),
            status_code=int(row["status_code"]),
            fetched_at=int(row["fetched_at"]),
            age_seconds=age,
        )

    def put(
        self,
        kind: str,
        key: str,
        body: Any,
        *,
        status_code: int = 200,
        now: int | None = None,
    ) -> None:
        """Write a successful response. Uses INSERT OR REPLACE.

        Only ``status_code=200`` is accepted here; permanent errors must use
        :meth:`put_error` so the call site is explicit about caching a failure.
        """
        if status_code != 200:
            raise ValueError(
                f"put() is for status 200; use put_error() for cacheable "
                f"error responses. Got status_code={status_code}"
            )
        self._validate_kind(kind)
        self._upsert(kind, key, body, status_code, now)

    def put_error(
        self,
        kind: str,
        key: str,
        status_code: int,
        error_body: Any,
        *,
        now: int | None = None,
    ) -> None:
        """Cache a permanent error (403 / 404). Refuses 5xx / others.

        The cached row replays the original error: a subsequent
        :meth:`get` returns a :class:`CacheHit` with the non-200
        ``status_code``, and the operations layer re-raises the
        appropriate :class:`HTTPError` subclass.
        """
        if status_code not in CACHEABLE_ERROR_STATUS_CODES:
            raise ValueError(
                f"only {sorted(CACHEABLE_ERROR_STATUS_CODES)} are cacheable as errors; "
                f"got status_code={status_code}. 5xx and transport errors must "
                f"propagate to the caller without being cached."
            )
        self._validate_kind(kind)
        self._upsert(kind, key, error_body, status_code, now)

    def purge(self, older_than_seconds: int, *, now: int | None = None) -> int:
        """Delete rows whose ``fetched_at`` is older than ``older_than_seconds``.
        Returns the number of rows deleted.
        """
        now_ts = int(time.time()) if now is None else int(now)
        cutoff = now_ts - int(older_than_seconds)
        cur = self._connect().execute(
            "DELETE FROM cached_objects WHERE fetched_at < ?",
            (cutoff,),
        )
        return int(cur.rowcount or 0)

    def stats(self) -> CacheStats:
        """Snapshot of hit/miss counters + on-disk size + row count."""
        size = 0
        rows = 0
        if self._db_path.exists():
            try:
                size = self._db_path.stat().st_size
            except OSError:
                size = 0
            try:
                row = self._connect().execute(
                    "SELECT COUNT(*) AS c FROM cached_objects"
                ).fetchone()
                rows = int(row["c"]) if row else 0
            except sqlite3.Error:
                rows = 0
        return CacheStats(
            hits=self._stats_hits,
            misses=self._stats_misses,
            writes=self._stats_writes,
            db_size_bytes=size,
            row_count=rows,
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Cache:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---- internals ----

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None puts sqlite3 in autocommit; combined with
        # `BEGIN`/`COMMIT` PRAGMAs as needed it's the simplest model for our
        # write pattern (one statement per put / purge / get).
        conn = sqlite3.connect(str(self._db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema(conn)
        self._conn = conn
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cached_objects (
                kind TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                fetched_at INTEGER NOT NULL,
                status_code INTEGER NOT NULL,
                body_json TEXT NOT NULL,
                PRIMARY KEY (kind, cache_key)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cached_fetched_at "
            "ON cached_objects(fetched_at)"
        )
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            return
        existing = int(row["value"])
        if existing != SCHEMA_VERSION:
            # No migration framework yet; refuse rather than silently misread.
            raise RuntimeError(
                f"cache schema mismatch: file at {self._db_path} is "
                f"v{existing}, library expects v{SCHEMA_VERSION}. "
                f"Delete the file or implement a migration."
            )

    def _upsert(
        self,
        kind: str,
        key: str,
        body: Any,
        status_code: int,
        now: int | None,
    ) -> None:
        body_json = json.dumps(body, separators=(",", ":"), default=str)
        now_ts = int(time.time()) if now is None else int(now)
        self._connect().execute(
            "INSERT OR REPLACE INTO cached_objects "
            "(kind, cache_key, fetched_at, status_code, body_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, key, now_ts, status_code, body_json),
        )
        self._stats_writes += 1

    def _validate_kind(self, kind: str) -> None:
        if kind not in VALID_KINDS:
            raise ValueError(
                f"unknown cache kind {kind!r}; valid: {sorted(VALID_KINDS)}"
            )
