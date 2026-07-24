"""Persistent cache of prompt -> generated filter, keyed for reuse across runs.

Most ``nfind`` prompts are repetitive: the same question, typed again days later.
Caching the generated filter next to the prompt that produced it lets a repeat prompt
skip the LLM call entirely -- faster, cheaper -- while the stored prompt/script pairs
double as a browsable, auditable history of every filter you have generated.

The cache is a single SQLite file under nfind's cache directory as ``queries.db`` (or
``$NFIND_QUERY_CACHE`` when set; see :mod:`nfind.paths`). Two matching strategies stack:

* **Normalized-exact** (always on, zero dependencies): the prompt is lower-cased and its
  whitespace collapsed, then compared for equality. Retyping the same question -- modulo
  case and spacing -- reuses the stored filter.
* **Semantic** (opt-in, needs the ``semantic`` extra): when an embedder is supplied and
  ``sqlite-vec`` is importable, a miss falls back to a cosine-nearest-neighbour lookup so
  differently-worded but equivalent prompts still hit. Injected as a plain callable, this
  module stays free of any provider/LLM knowledge.

A cached filter is only reused when the *generation-affecting* parameters match too
(``macos_meta`` and ``extract`` change the code the model is asked to produce), so a
cache hit always replays a filter generated for the same mode.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .paths import user_dir
from .runtimes import GeneratedFilter

# Injected embedder: maps a prompt string to a dense vector. Kept abstract so the cache
# never imports the LLM layer; :mod:`nfind.embedding` builds one from a model selector.
Embedder = Callable[[str], list[float]]

DEFAULT_SEMANTIC_THRESHOLD = 0.15
"""Maximum cosine *distance* (1 - cosine similarity) for a semantic hit to be reused."""

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_prompt(prompt: str) -> str:
    """Fold a prompt to its match key: trimmed, lower-cased, whitespace collapsed."""
    return _WHITESPACE_RE.sub(" ", prompt.strip().lower())


def _now() -> str:
    """Current UTC time as an ISO-8601 string (stable, sortable, timezone-explicit)."""
    return datetime.now(UTC).isoformat()


@dataclass
class CacheEntry:
    """One stored prompt and the filter generated for it, plus provenance/usage."""

    id: int
    prompt: str
    code: str
    runtime: str
    dependencies: list[str]
    model: str
    macos_meta: bool
    extract: bool
    created_at: str
    used_count: int
    last_used_at: str | None
    # Cosine distance to the query prompt on a semantic hit; None for an exact hit.
    distance: float | None = None

    def to_filter(self) -> GeneratedFilter:
        """Rebuild the runnable :class:`GeneratedFilter` from the stored columns."""
        return GeneratedFilter(
            code=self.code,
            dependencies=list(self.dependencies),
            runtime=self.runtime,
        )


def default_cache_path() -> Path:
    """Location of the query-cache database (``$NFIND_QUERY_CACHE`` wins)."""
    override = os.environ.get("NFIND_QUERY_CACHE")
    if override:
        return Path(override)
    return user_dir("cache") / "queries.db"


def _row_to_entry(row: sqlite3.Row, *, distance: float | None = None) -> CacheEntry:
    return CacheEntry(
        id=row["id"],
        prompt=row["prompt"],
        code=row["code"],
        runtime=row["runtime"],
        dependencies=json.loads(row["dependencies"]),
        model=row["model"],
        macos_meta=bool(row["macos_meta"]),
        extract=bool(row["extract"]),
        created_at=row["created_at"],
        used_count=row["used_count"],
        last_used_at=row["last_used_at"],
        distance=distance,
    )


class QueryCache:
    """SQLite-backed store of prompt/filter pairs with exact and optional semantic reuse.

    ``embedder`` and ``embedding_model`` enable the semantic path; without them (or
    without ``sqlite-vec`` installed) only normalized-exact matching is used. The database
    file and its parent directory are created lazily on first write.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        embedder: Embedder | None = None,
        embedding_model: str | None = None,
        threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
    ) -> None:
        self.path = path if path is not None else default_cache_path()
        self._embedder = embedder
        self._embedding_model = embedding_model
        self._threshold = threshold
        self._conn: sqlite3.Connection | None = None
        # Set once we know whether the vector table can be used with this embedder.
        self._vec_enabled: bool | None = None

    # -- connection / schema ------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS queries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt TEXT NOT NULL,
                    prompt_norm TEXT NOT NULL,
                    code TEXT NOT NULL,
                    runtime TEXT NOT NULL,
                    dependencies TEXT NOT NULL,
                    model TEXT NOT NULL,
                    macos_meta INTEGER NOT NULL,
                    extract INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    used_count INTEGER NOT NULL DEFAULT 0,
                    last_used_at TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_queries_norm ON queries(prompt_norm)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.commit()
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> QueryCache:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- semantic (vector) support -----------------------------------------

    def _semantic_ready(self, conn: sqlite3.Connection, dim: int) -> bool:
        """Load sqlite-vec and ensure a vector table matching this embedder's model/dim.

        Best-effort: if the extension is missing, or the stored table was built for a
        different embedding model/dimension, the semantic path is disabled and the cache
        falls back to exact matching. Cached once per ``QueryCache`` instance.
        """
        if self._vec_enabled is not None:
            return self._vec_enabled
        self._vec_enabled = self._init_vec_table(conn, dim)
        return self._vec_enabled

    def _init_vec_table(self, conn: sqlite3.Connection, dim: int) -> bool:
        try:
            import sqlite_vec
        except ImportError:
            return False
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except (AttributeError, sqlite3.OperationalError):
            # Some Python builds disable loadable extensions entirely.
            return False

        stored_model = self._meta_get(conn, "embedding_model")
        stored_dim = self._meta_get(conn, "embedding_dim")
        if stored_model is not None and (
            stored_model != self._embedding_model or stored_dim != str(dim)
        ):
            # The vector index was built for a different embedder; comparing across models
            # is meaningless, so skip semantic rather than return wrong matches.
            return False
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS query_vectors "
            f"USING vec0(embedding float[{dim}] distance_metric=cosine)"
        )
        if stored_model is None:
            self._meta_set(conn, "embedding_model", self._embedding_model or "")
            self._meta_set(conn, "embedding_dim", str(dim))
            conn.commit()
        return True

    def _meta_get(self, conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def _meta_set(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # -- lookup / store -----------------------------------------------------

    def lookup(self, prompt: str, *, macos_meta: bool, extract: bool) -> CacheEntry | None:
        """Return a reusable entry for ``prompt`` in the given mode, or ``None`` on a miss.

        Tries normalized-exact first; if that misses and a semantic embedder is available,
        falls back to the nearest stored prompt within the cosine-distance threshold.
        Entries generated in a different mode (``macos_meta``/``extract``) never match.
        """
        conn = self._connect()
        norm = normalize_prompt(prompt)
        row = conn.execute(
            "SELECT * FROM queries WHERE prompt_norm = ? AND macos_meta = ? AND extract = ? "
            "ORDER BY id DESC LIMIT 1",
            (norm, int(macos_meta), int(extract)),
        ).fetchone()
        if row is not None:
            return _row_to_entry(row)
        return self._semantic_lookup(conn, prompt, macos_meta=macos_meta, extract=extract)

    def _semantic_lookup(
        self, conn: sqlite3.Connection, prompt: str, *, macos_meta: bool, extract: bool
    ) -> CacheEntry | None:
        if self._embedder is None:
            return None
        vector = self._embedder(prompt)
        if not self._semantic_ready(conn, len(vector)):
            return None
        import sqlite_vec

        # KNN scan a few nearest neighbours, then keep the closest whose mode matches and
        # whose distance is within threshold. Over-fetch so mode filtering has candidates.
        rows = conn.execute(
            """
            SELECT q.*, v.distance AS distance
            FROM query_vectors AS v
            JOIN queries AS q ON q.id = v.rowid
            WHERE v.embedding MATCH ? AND k = 10
            ORDER BY v.distance
            """,
            (sqlite_vec.serialize_float32(vector),),
        ).fetchall()
        for row in rows:
            if bool(row["macos_meta"]) != macos_meta or bool(row["extract"]) != extract:
                continue
            if row["distance"] <= self._threshold:
                return _row_to_entry(row, distance=float(row["distance"]))
            break  # rows are distance-sorted: the first mode match is the closest
        return None

    def store(
        self,
        prompt: str,
        generated: GeneratedFilter,
        *,
        model: str,
        macos_meta: bool,
        extract: bool,
    ) -> CacheEntry:
        """Persist a freshly generated filter and return its :class:`CacheEntry`."""
        conn = self._connect()
        created = _now()
        cursor = conn.execute(
            "INSERT INTO queries("
            "prompt, prompt_norm, code, runtime, dependencies, model, macos_meta, extract, "
            "created_at, used_count, last_used_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)",
            (
                prompt,
                normalize_prompt(prompt),
                generated.code,
                generated.runtime,
                json.dumps(generated.dependencies),
                model,
                int(macos_meta),
                int(extract),
                created,
            ),
        )
        entry_id = int(cursor.lastrowid or 0)
        self._store_vector(conn, entry_id, prompt)
        conn.commit()
        return CacheEntry(
            id=entry_id,
            prompt=prompt,
            code=generated.code,
            runtime=generated.runtime,
            dependencies=list(generated.dependencies),
            model=model,
            macos_meta=macos_meta,
            extract=extract,
            created_at=created,
            used_count=0,
            last_used_at=None,
        )

    def _store_vector(self, conn: sqlite3.Connection, entry_id: int, prompt: str) -> None:
        if self._embedder is None:
            return
        vector = self._embedder(prompt)
        if not self._semantic_ready(conn, len(vector)):
            return
        import sqlite_vec

        conn.execute(
            "INSERT INTO query_vectors(rowid, embedding) VALUES(?, ?)",
            (entry_id, sqlite_vec.serialize_float32(vector)),
        )

    def record_use(self, entry_id: int) -> None:
        """Bump the usage counter and last-used timestamp after a cache hit."""
        conn = self._connect()
        conn.execute(
            "UPDATE queries SET used_count = used_count + 1, last_used_at = ? WHERE id = ?",
            (_now(), entry_id),
        )
        conn.commit()

    # -- management ---------------------------------------------------------

    def all(self) -> list[CacheEntry]:
        """Every stored entry, newest first."""
        conn = self._connect()
        rows = conn.execute("SELECT * FROM queries ORDER BY id DESC").fetchall()
        return [_row_to_entry(row) for row in rows]

    def get(self, entry_id: int) -> CacheEntry | None:
        """The entry with ``entry_id``, or ``None``."""
        conn = self._connect()
        row = conn.execute("SELECT * FROM queries WHERE id = ?", (entry_id,)).fetchone()
        return None if row is None else _row_to_entry(row)

    def delete(self, entry_ids: Iterable[int]) -> int:
        """Delete the given entries by id; return how many rows were removed.

        Remaining entries keep their ids -- ids are stable identifiers, not positions, so
        deleting one leaves a gap rather than renumbering the others. Any matching
        semantic-index rows are removed too. Unknown ids are ignored.
        """
        ids = list(dict.fromkeys(entry_ids))
        if not ids:
            return 0
        conn = self._connect()
        placeholders = ",".join("?" * len(ids))
        cursor = conn.execute(f"DELETE FROM queries WHERE id IN ({placeholders})", ids)
        removed = cursor.rowcount
        if self._table_exists(conn, "query_vectors"):
            conn.execute(f"DELETE FROM query_vectors WHERE rowid IN ({placeholders})", ids)
        conn.commit()
        return int(removed)

    def clear(self) -> int:
        """Delete all entries; return how many were removed."""
        conn = self._connect()
        count = conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0]
        conn.execute("DELETE FROM queries")
        if self._table_exists(conn, "query_vectors"):
            conn.execute("DELETE FROM query_vectors")
        conn.commit()
        return int(count)

    def _table_exists(self, conn: sqlite3.Connection, name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
            (name,),
        ).fetchone()
        return row is not None


def iter_entry_summaries(entries: Iterable[CacheEntry]) -> Iterable[str]:
    """One-line ``id  created  model  prompt`` summaries for ``nfind cache list``."""
    for entry in entries:
        created = entry.created_at[:19]
        prompt = entry.prompt if len(entry.prompt) <= 60 else entry.prompt[:57] + "..."
        yield f"{entry.id:>4}  {created}  {entry.model:<28}  {prompt}"
