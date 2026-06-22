"""Tests for find-style enumeration filters: --exclude/ignore, --max-depth, --print0."""

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from nfind import backend as MODULE
from nfind import cli


@pytest.fixture
def tree(tmp_path):
    """A small tree with ignorable dirs, nested files, and a couple of extensions."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("x")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x")
    (tmp_path / "src" / "deep").mkdir()
    (tmp_path / "src" / "deep" / "b.py").write_text("x")
    (tmp_path / "keep.log").write_text("x")
    (tmp_path / "main.py").write_text("x")
    return tmp_path


def _names(paths):
    return sorted(p.removeprefix("/data") for p in paths)


# --- enumerate_paths ------------------------------------------------------------


def test_default_ignores_skip_vcs_and_dependency_dirs(tree):
    paths, _ = MODULE.enumerate_paths(tree)
    names = _names(paths)
    assert "/.git" not in names and "/.git/config" not in names
    assert "/node_modules" not in names and "/node_modules/lib.js" not in names
    assert "/main.py" in names and "/src/a.py" in names


def test_no_default_ignores_includes_everything(tree):
    paths, _ = MODULE.enumerate_paths(tree, use_default_ignores=False)
    names = _names(paths)
    assert "/.git" in names and "/.git/config" in names
    assert "/node_modules/lib.js" in names


def test_exclude_globs_prune_files_and_directories(tree):
    paths, _ = MODULE.enumerate_paths(tree, exclude=["*.log", "deep"])
    names = _names(paths)
    assert "/keep.log" not in names  # file glob
    assert "/src/deep" not in names and "/src/deep/b.py" not in names  # pruned subtree
    assert "/main.py" in names and "/src/a.py" in names


def test_exclude_matches_relative_path(tree):
    # A pattern with a slash matches the root-relative path, not just the basename.
    paths, _ = MODULE.enumerate_paths(tree, exclude=["src/*"])
    names = _names(paths)
    assert "/src/a.py" not in names and "/src/deep" not in names
    assert "/src" in names  # the directory itself is above the match


def test_max_depth_limits_descent(tree):
    paths, _ = MODULE.enumerate_paths(tree, max_depth=1)
    names = _names(paths)
    assert names == ["/keep.log", "/main.py", "/src"]  # direct children only


def test_max_depth_two_reaches_second_level(tree):
    paths, _ = MODULE.enumerate_paths(tree, max_depth=2)
    names = _names(paths)
    assert "/src/a.py" in names and "/src/deep" in names
    assert "/src/deep/b.py" not in names  # depth 3 pruned


def test_max_depth_must_be_positive(tree):
    with pytest.raises(ValueError, match="max_depth must be at least 1"):
        MODULE.enumerate_paths(tree, max_depth=0)


def test_excluded_dirs_are_not_descended(tree):
    # Pruning a directory must drop its whole subtree, not just the directory entry.
    paths, _ = MODULE.enumerate_paths(tree, exclude=["src"])
    names = _names(paths)
    assert not any(name.startswith("/src") for name in names)


# --- CLI wiring -----------------------------------------------------------------


def _run(args, records=None):
    runner = CliRunner()
    with patch.object(cli.backend, "search", return_value=records or []) as search:
        result = runner.invoke(cli.app, ["prompt", "/tmp", *args])
    return result, search


def test_cli_threads_enumeration_flags_to_search():
    result, search = _run(
        ["--exclude", "*.min.js", "--exclude", "build", "--max-depth", "3", "--no-ignore"]
    )
    assert result.exit_code == 0
    kwargs = search.call_args.kwargs
    assert kwargs["exclude"] == ("*.min.js", "build")
    assert kwargs["max_depth"] == 3
    assert kwargs["use_default_ignores"] is False


def test_cli_default_enumeration_flags():
    result, search = _run([])
    kwargs = search.call_args.kwargs
    assert kwargs["exclude"] == ()
    assert kwargs["max_depth"] is None
    assert kwargs["use_default_ignores"] is True


def test_cli_print0_separates_with_nul():
    result, _ = _run(["--print0"], records=[{"path": "/a b"}, {"path": "/c"}])
    assert result.exit_code == 0
    assert result.stdout == "/a b\0/c\0"


def test_cli_print0_conflicts_with_json():
    runner = CliRunner()
    result = runner.invoke(cli.app, ["prompt", "/tmp", "--print0", "--json"])
    assert result.exit_code == 2
    assert "--print0 cannot be combined" in result.output


def test_cli_print0_conflicts_with_verbose():
    runner = CliRunner()
    result = runner.invoke(cli.app, ["prompt", "/tmp", "--print0", "--verbose"])
    assert result.exit_code == 2


def test_cli_rejects_nonpositive_max_depth():
    runner = CliRunner()
    result = runner.invoke(cli.app, ["prompt", "/tmp", "--max-depth", "0"])
    assert result.exit_code == 2
    assert "--max-depth must be at least 1" in result.output


def test_cli_run_threads_enumeration_flags_to_run_saved(tmp_path):
    script = tmp_path / "f.py"
    script.write_text("def filter_paths(paths): return paths")
    runner = CliRunner()
    with patch.object(cli.backend, "run_saved", return_value=[]) as run_saved:
        result = runner.invoke(
            cli.app, ["--run", str(script), str(tmp_path), "--exclude", "tmp", "--max-depth", "2"]
        )
    assert result.exit_code == 0
    kwargs = run_saved.call_args.kwargs
    assert kwargs["exclude"] == ("tmp",)
    assert kwargs["max_depth"] == 2
