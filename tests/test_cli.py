"""Unit tests for the CLI that need no sandbox backend or LLM."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import nfind
from nfind import cli
from nfind.query_cache import QueryCache
from nfind.runtimes import GeneratedFilter


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_flag_prints_version_and_exits(flag: str) -> None:
    result = CliRunner().invoke(cli.app, [flag])
    assert result.exit_code == 0
    assert result.output.strip() == f"nfind {nfind.__version__}"


# -- default-command routing -----------------------------------------------


# Explicit help exits 0; a bare `nfind` keeps the historical no-args-help exit code 2.
@pytest.mark.parametrize("args,code", [(["-h"], 0), (["--help"], 0), ([], 2)])
def test_top_level_help_shows_search_help_with_cache_epilog(args: list[str], code: int) -> None:
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == code
    # The primary (search) options are shown at the top level...
    assert "PROMPT" in result.output
    assert "--no-cache" in result.output
    # ...plus a pointer to the cache subcommand.
    assert "nfind cache" in result.output


def test_cache_help_lists_verbs() -> None:
    result = CliRunner().invoke(cli.app, ["cache", "-h"])
    assert result.exit_code == 0
    for verb in ("list", "show", "clear"):
        assert verb in result.output


# -- cache subcommands ------------------------------------------------------


def _populate(path: Path) -> tuple[int, int]:
    with QueryCache(path) as cache:
        code = "def filter_paths(paths):\n    return paths"
        first = cache.store(
            "find pdfs",
            GeneratedFilter(code=code, dependencies=["pypdf"]),
            model="openai/gpt-5.4",
            macos_meta=False,
            extract=False,
        )
        second = cache.store(
            "find images",
            GeneratedFilter(code=code),
            model="openai/gpt-5.4",
            macos_meta=True,
            extract=False,
        )
    return first.id, second.id


@pytest.fixture
def cache_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "queries.db"
    monkeypatch.setenv("NFIND_QUERY_CACHE", str(db))
    return db


def test_cache_list_empty(cache_db: Path) -> None:
    result = CliRunner().invoke(cli.app, ["cache", "list"])
    assert result.exit_code == 0
    assert "empty" in result.stderr


def test_cache_list_shows_entries(cache_db: Path) -> None:
    _populate(cache_db)
    result = CliRunner().invoke(cli.app, ["cache", "list"])
    assert result.exit_code == 0
    assert "find pdfs" in result.output
    assert "find images" in result.output


def test_cache_show_prints_entry_and_code(cache_db: Path) -> None:
    first_id, _ = _populate(cache_db)
    result = CliRunner().invoke(cli.app, ["cache", "show", str(first_id)])
    assert result.exit_code == 0
    assert "find pdfs" in result.stderr
    assert "pypdf" in result.stderr
    assert "filter_paths" in result.output  # the code goes to stdout


def test_cache_show_missing_id_errors(cache_db: Path) -> None:
    result = CliRunner().invoke(cli.app, ["cache", "show", "999"])
    assert result.exit_code == 1
    assert "no cache entry" in result.stderr


def test_cache_delete_removes_entry(cache_db: Path) -> None:
    first_id, second_id = _populate(cache_db)
    result = CliRunner().invoke(cli.app, ["cache", "delete", str(first_id)])
    assert result.exit_code == 0
    assert "deleted 1" in result.stderr
    with QueryCache(cache_db) as cache:
        assert [e.id for e in cache.all()] == [second_id]


def test_cache_delete_multiple(cache_db: Path) -> None:
    first_id, second_id = _populate(cache_db)
    result = CliRunner().invoke(cli.app, ["cache", "delete", str(first_id), str(second_id)])
    assert result.exit_code == 0
    assert "deleted 2" in result.stderr
    with QueryCache(cache_db) as cache:
        assert cache.all() == []


def test_cache_delete_unknown_id_warns_and_exits_nonzero(cache_db: Path) -> None:
    _populate(cache_db)
    result = CliRunner().invoke(cli.app, ["cache", "delete", "999"])
    assert result.exit_code == 1
    assert "no cache entry" in result.stderr
    assert "deleted 0" in result.stderr


def test_cache_delete_mixed_known_and_unknown(cache_db: Path) -> None:
    first_id, _ = _populate(cache_db)
    result = CliRunner().invoke(cli.app, ["cache", "delete", str(first_id), "999"])
    assert result.exit_code == 0
    assert "no cache entry with id 999" in result.stderr
    assert "deleted 1" in result.stderr


def test_cache_clear_requires_confirmation(cache_db: Path) -> None:
    _populate(cache_db)
    result = CliRunner().invoke(cli.app, ["cache", "clear"], input="n\n")
    assert result.exit_code == 130
    with QueryCache(cache_db) as cache:
        assert len(cache.all()) == 2  # nothing deleted


def test_cache_clear_yes_deletes_all(cache_db: Path) -> None:
    _populate(cache_db)
    result = CliRunner().invoke(cli.app, ["cache", "clear", "--yes"])
    assert result.exit_code == 0
    assert "cleared 2" in result.stderr
    with QueryCache(cache_db) as cache:
        assert cache.all() == []


# -- cache construction helper ---------------------------------------------


def test_build_query_cache_disabled_returns_none() -> None:
    assert (
        cli._build_query_cache(enabled=False, semantic=False, embedding_model=None, threshold=None)
        is None
    )


def test_build_query_cache_warns_when_semantic_extra_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    cache = cli._build_query_cache(
        enabled=True, semantic=True, embedding_model=None, threshold=None
    )
    assert cache is not None
    err = capsys.readouterr().err
    assert "semantic" in err and "extra" in err
