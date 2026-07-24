"""Unit tests for the persistent prompt/filter query cache."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from nfind.query_cache import (
    DEFAULT_SEMANTIC_THRESHOLD,
    QueryCache,
    default_cache_path,
    iter_entry_summaries,
    normalize_prompt,
)
from nfind.runtimes import GeneratedFilter


def _gen(code: str = "def filter_paths(paths):\n    return paths", deps=(), runtime="python"):
    return GeneratedFilter(code=code, dependencies=list(deps), runtime=runtime)


# -- normalization ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  Find PDFs  ", "find pdfs"),
        ("Find\tPDFs\nnow", "find pdfs now"),
        ("MiXeD   CASE", "mixed case"),
    ],
)
def test_normalize_prompt(raw: str, expected: str) -> None:
    assert normalize_prompt(raw) == expected


# -- path resolution --------------------------------------------------------


def test_default_cache_path_honors_override(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "custom.db"
    monkeypatch.setenv("NFIND_QUERY_CACHE", str(target))
    assert default_cache_path() == target


def test_default_cache_path_uses_cache_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("NFIND_QUERY_CACHE", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert default_cache_path() == tmp_path / "nfind" / "queries.db"


# -- exact matching / storage ----------------------------------------------


def test_store_then_exact_lookup_roundtrip(tmp_path: Path) -> None:
    with QueryCache(tmp_path / "q.db") as cache:
        entry = cache.store(
            "Find PDFs modified last week",
            _gen(deps=["pypdf"]),
            model="openai/gpt-5.4",
            macos_meta=False,
            extract=False,
        )
        assert entry.id == 1
        # Case- and whitespace-insensitive re-typing hits.
        hit = cache.lookup("find   pdfs MODIFIED last week", macos_meta=False, extract=False)
        assert hit is not None
        assert hit.id == entry.id
        assert hit.dependencies == ["pypdf"]
        assert hit.distance is None
        filt = hit.to_filter()
        assert filt.runtime == "python"
        assert filt.dependencies == ["pypdf"]


def test_lookup_miss_returns_none(tmp_path: Path) -> None:
    with QueryCache(tmp_path / "q.db") as cache:
        cache.store("find pdfs", _gen(), model="m", macos_meta=False, extract=False)
        assert cache.lookup("something else entirely", macos_meta=False, extract=False) is None


@pytest.mark.parametrize(
    "store_meta,store_extract,look_meta,look_extract",
    [
        (False, False, True, False),
        (False, False, False, True),
        (True, True, True, False),
    ],
)
def test_mode_mismatch_does_not_hit(
    tmp_path: Path, store_meta, store_extract, look_meta, look_extract
) -> None:
    with QueryCache(tmp_path / "q.db") as cache:
        cache.store("find pdfs", _gen(), model="m", macos_meta=store_meta, extract=store_extract)
        assert cache.lookup("find pdfs", macos_meta=look_meta, extract=look_extract) is None


def test_record_use_bumps_counter_and_timestamp(tmp_path: Path) -> None:
    with QueryCache(tmp_path / "q.db") as cache:
        entry = cache.store("find pdfs", _gen(), model="m", macos_meta=False, extract=False)
        assert entry.used_count == 0 and entry.last_used_at is None
        cache.record_use(entry.id)
        cache.record_use(entry.id)
        refreshed = cache.get(entry.id)
        assert refreshed is not None
        assert refreshed.used_count == 2
        assert refreshed.last_used_at is not None


def test_all_orders_newest_first_and_get_and_clear(tmp_path: Path) -> None:
    with QueryCache(tmp_path / "q.db") as cache:
        first = cache.store("prompt one", _gen(), model="m", macos_meta=False, extract=False)
        second = cache.store("prompt two", _gen(), model="m", macos_meta=False, extract=False)
        entries = cache.all()
        assert [e.id for e in entries] == [second.id, first.id]
        got = cache.get(first.id)
        assert got is not None and got.prompt == "prompt one"
        assert cache.get(9999) is None
        assert cache.clear() == 2
        assert cache.all() == []


def test_delete_removes_entries_without_renumbering(tmp_path: Path) -> None:
    with QueryCache(tmp_path / "q.db") as cache:
        first = cache.store("one", _gen(), model="m", macos_meta=False, extract=False)
        second = cache.store("two", _gen(), model="m", macos_meta=False, extract=False)
        third = cache.store("three", _gen(), model="m", macos_meta=False, extract=False)
        assert (first.id, second.id, third.id) == (1, 2, 3)
        assert cache.delete([second.id]) == 1
        # Survivors keep their ids -- a gap, never a renumber.
        assert [e.id for e in cache.all()] == [3, 1]
        assert cache.get(2) is None
        survivor = cache.get(3)
        assert survivor is not None and survivor.prompt == "three"


def test_delete_multiple_and_ignores_unknown_ids(tmp_path: Path) -> None:
    with QueryCache(tmp_path / "q.db") as cache:
        a = cache.store("a", _gen(), model="m", macos_meta=False, extract=False)
        b = cache.store("b", _gen(), model="m", macos_meta=False, extract=False)
        # Duplicates in the request and an unknown id don't inflate the count.
        assert cache.delete([a.id, a.id, b.id, 999]) == 2
        assert cache.all() == []


def test_delete_empty_is_noop(tmp_path: Path) -> None:
    with QueryCache(tmp_path / "q.db") as cache:
        cache.store("a", _gen(), model="m", macos_meta=False, extract=False)
        assert cache.delete([]) == 0
        assert len(cache.all()) == 1


def test_newest_entry_wins_on_duplicate_key(tmp_path: Path) -> None:
    # --force can store a second entry with the same normalized prompt; lookup returns
    # the most recent one.
    with QueryCache(tmp_path / "q.db") as cache:
        cache.store("find pdfs", _gen(code="OLD"), model="m", macos_meta=False, extract=False)
        newer = cache.store(
            "find pdfs", _gen(code="NEW"), model="m", macos_meta=False, extract=False
        )
        hit = cache.lookup("find pdfs", macos_meta=False, extract=False)
        assert hit is not None
        assert hit.id == newer.id
        assert hit.code == "NEW"


def test_persists_across_connections(tmp_path: Path) -> None:
    db = tmp_path / "q.db"
    with QueryCache(db) as cache:
        cache.store("find pdfs", _gen(), model="m", macos_meta=False, extract=False)
    with QueryCache(db) as reopened:
        assert reopened.lookup("find pdfs", macos_meta=False, extract=False) is not None


def test_iter_entry_summaries_truncates_long_prompts(tmp_path: Path) -> None:
    with QueryCache(tmp_path / "q.db") as cache:
        cache.store("x" * 100, _gen(), model="m", macos_meta=False, extract=False)
        lines = list(iter_entry_summaries(cache.all()))
        assert len(lines) == 1
        assert "..." in lines[0]


# -- semantic matching (optional: needs sqlite-vec) -------------------------

sqlite_vec = pytest.importorskip("sqlite_vec")

_VOCAB = ["pdf", "pdfs", "file", "files", "modified", "changed", "week", "recent", "image", "photo"]


def _fake_embed(text: str) -> list[float]:
    lowered = text.lower()
    vector = [float(lowered.count(word)) for word in _VOCAB]
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


def _semantic_cache(path: Path, *, threshold: float, model: str = "fake/emb") -> QueryCache:
    return QueryCache(path, embedder=_fake_embed, embedding_model=model, threshold=threshold)


def test_semantic_lookup_hits_similar_prompt(tmp_path: Path) -> None:
    with _semantic_cache(tmp_path / "q.db", threshold=0.7) as cache:
        cache.store(
            "pdf files modified this week", _gen(), model="m", macos_meta=False, extract=False
        )
        hit = cache.lookup("pdfs changed recent week", macos_meta=False, extract=False)
        assert hit is not None
        assert hit.distance is not None and hit.distance <= 0.7


def test_semantic_lookup_respects_threshold(tmp_path: Path) -> None:
    with _semantic_cache(tmp_path / "q.db", threshold=0.05) as cache:
        cache.store(
            "pdf files modified this week", _gen(), model="m", macos_meta=False, extract=False
        )
        # Distinct wording is beyond the tight threshold -> no reuse.
        assert cache.lookup("pdfs changed recent week", macos_meta=False, extract=False) is None


def test_semantic_lookup_respects_mode(tmp_path: Path) -> None:
    with _semantic_cache(tmp_path / "q.db", threshold=0.9) as cache:
        cache.store(
            "pdf files modified this week", _gen(), model="m", macos_meta=False, extract=False
        )
        assert cache.lookup("pdfs changed recent week", macos_meta=True, extract=False) is None


def test_semantic_disabled_on_embedding_model_change(tmp_path: Path) -> None:
    db = tmp_path / "q.db"
    with _semantic_cache(db, threshold=0.9, model="model/a") as cache:
        cache.store("pdf files modified", _gen(), model="m", macos_meta=False, extract=False)
    # A different embedding model can't be compared against stored vectors: semantic is
    # skipped, but normalized-exact matching still works.
    with _semantic_cache(db, threshold=0.9, model="model/b") as reopened:
        assert reopened.lookup("pdfs changed recent", macos_meta=False, extract=False) is None
        assert reopened.lookup("PDF files modified", macos_meta=False, extract=False) is not None


def test_delete_removes_semantic_vector(tmp_path: Path) -> None:
    with _semantic_cache(tmp_path / "q.db", threshold=0.9) as cache:
        entry = cache.store(
            "pdf files modified this week", _gen(), model="m", macos_meta=False, extract=False
        )
        assert cache.lookup("pdfs changed recent week", macos_meta=False, extract=False) is not None
        assert cache.delete([entry.id]) == 1
        # With the vector row gone, the semantic lookup no longer matches.
        assert cache.lookup("pdfs changed recent week", macos_meta=False, extract=False) is None


def test_default_threshold_constant() -> None:
    assert 0.0 < DEFAULT_SEMANTIC_THRESHOLD < 1.0
