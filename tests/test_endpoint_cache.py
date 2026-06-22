"""Tests for the best-effort model-endpoint cache."""

from __future__ import annotations

from pfind import endpoint_cache


def test_round_trip(tmp_path, monkeypatch):
    cache = tmp_path / "endpoints.json"
    monkeypatch.setenv("PFIND_ENDPOINT_CACHE", str(cache))
    assert endpoint_cache.get_endpoint("openai/o3") is None
    endpoint_cache.set_endpoint("openai/o3", "responses")
    assert endpoint_cache.get_endpoint("openai/o3") == "responses"
    # Persisted as JSON keyed by the full selector.
    assert '"openai/o3": "responses"' in cache.read_text()


def test_default_path_under_cache_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("PFIND_ENDPOINT_CACHE", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    endpoint_cache.set_endpoint("openai/o3", "responses")
    assert (tmp_path / "pfind" / "model-endpoints.json").exists()


def test_set_creates_parent_directory(tmp_path, monkeypatch):
    cache = tmp_path / "nested" / "deeper" / "endpoints.json"
    monkeypatch.setenv("PFIND_ENDPOINT_CACHE", str(cache))
    endpoint_cache.set_endpoint("openai/o3", "responses")
    assert cache.exists()


def test_read_tolerates_missing_and_malformed(tmp_path, monkeypatch):
    cache = tmp_path / "endpoints.json"
    monkeypatch.setenv("PFIND_ENDPOINT_CACHE", str(cache))
    assert endpoint_cache.get_endpoint("x") is None  # missing file
    cache.write_text("not json {")
    assert endpoint_cache.get_endpoint("x") is None  # malformed
    cache.write_text('["a", "list"]')
    assert endpoint_cache.get_endpoint("x") is None  # not an object


def test_read_drops_non_string_entries(tmp_path, monkeypatch):
    cache = tmp_path / "endpoints.json"
    monkeypatch.setenv("PFIND_ENDPOINT_CACHE", str(cache))
    cache.write_text('{"openai/o3": "responses", "bad": 5, "x": null}')
    assert endpoint_cache.get_endpoint("openai/o3") == "responses"
    assert endpoint_cache.get_endpoint("bad") is None


def test_set_skips_write_when_unchanged(tmp_path, monkeypatch):
    cache = tmp_path / "endpoints.json"
    monkeypatch.setenv("PFIND_ENDPOINT_CACHE", str(cache))
    endpoint_cache.set_endpoint("openai/o3", "responses")
    before = cache.stat().st_mtime_ns
    endpoint_cache.set_endpoint("openai/o3", "responses")
    assert cache.stat().st_mtime_ns == before  # no rewrite for an identical value


def test_set_is_best_effort_on_io_error(tmp_path, monkeypatch):
    # A path whose parent is a file cannot be created; the write must swallow the error.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file")
    monkeypatch.setenv("PFIND_ENDPOINT_CACHE", str(blocker / "endpoints.json"))
    endpoint_cache.set_endpoint("openai/o3", "responses")  # must not raise
    assert endpoint_cache.get_endpoint("openai/o3") is None
