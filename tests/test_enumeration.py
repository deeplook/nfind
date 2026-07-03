"""Tests for find-style enumeration filters: --exclude/ignore, --max-depth, --print0."""

import os
from unittest.mock import Mock, patch

import pytest
from typer.testing import CliRunner

from nfind import cli
from nfind import enumeration as MODULE
from nfind.command_plan import GeneratedSearchRequest

# Identity mounting (container path == host path) is POSIX-only: a Linux container needs a
# POSIX mount target, so Windows falls back to /data and these expectations don't hold there.
posix_only = pytest.mark.skipif(os.name != "posix", reason="identity mounting is POSIX-only")


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


# --- enumerate_roots: identity mounting (container path == host path) ------------


@posix_only
def test_enumerate_roots_single_dir_mounts_at_host_path(tmp_path):
    # A single safe root is mounted at its own host path; the mapping is the identity.
    (tmp_path / "a.txt").write_text("x")
    root = tmp_path.resolve()
    host = str(root / "a.txt")
    paths, mapping, mounts = MODULE.enumerate_roots([tmp_path])
    assert host in paths
    assert mapping[host] == host  # identity
    assert len(mounts) == 1
    assert mounts[0].source == root and mounts[0].target == str(root)


@posix_only
def test_enumerate_roots_disjoint_roots_use_identity_paths(tmp_path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    # Identically named files in disjoint roots keep distinct host paths -- no collision,
    # so no /data namespacing is needed.
    (first / "dup.txt").write_text("x")
    (second / "dup.txt").write_text("y")

    paths, mapping, mounts = MODULE.enumerate_roots([first, second])

    rfirst, rsecond = first.resolve(), second.resolve()
    first_dup = str(rfirst / "dup.txt")
    second_dup = str(rsecond / "dup.txt")
    assert first_dup in paths and second_dup in paths
    assert mapping[first_dup] == first_dup and mapping[second_dup] == second_dup
    assert [mount.target for mount in mounts] == [str(rfirst), str(rsecond)]
    assert [mount.source for mount in mounts] == [rfirst, rsecond]
    assert not any(p.startswith("/data") for p in paths)


@posix_only
def test_enumerate_roots_single_file_root_mounts_at_host_path(tmp_path):
    # A file root is a single entry mounted at its own host path under identity mounting.
    file_path = tmp_path / "worker.py"
    file_path.write_text("x")
    host = str(file_path.resolve())
    paths, mapping, mounts = MODULE.enumerate_roots([file_path])
    assert paths == [host]
    assert mapping[host] == host
    assert len(mounts) == 1
    assert mounts[0].source == file_path.resolve()
    assert mounts[0].target == host  # the file itself, not a directory
    assert mounts[0].read_only is True


@posix_only
def test_enumerate_roots_mixed_file_and_directory_identity(tmp_path):
    file_root = tmp_path / "solo.py"
    file_root.write_text("x")
    dir_root = tmp_path / "pkg"
    dir_root.mkdir()
    (dir_root / "a.py").write_text("x")

    paths, mapping, mounts = MODULE.enumerate_roots([file_root, dir_root])

    solo = str(file_root.resolve())
    nested = str(dir_root.resolve() / "a.py")
    assert solo in paths and nested in paths
    assert mapping[solo] == solo and mapping[nested] == nested
    assert [m.target for m in mounts] == [solo, str(dir_root.resolve())]


# --- enumerate_roots: fallback to /data namespacing ------------------------------


def test_enumerate_roots_overlapping_roots_fall_back_to_namespacing(tmp_path):
    # A root and a descendant of it would collide under identity mounting (their trees and
    # mapping keys overlap), so the whole set falls back to namespaced /data mountpoints.
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / "p.txt").write_text("x")
    (child / "c.txt").write_text("y")

    paths, mapping, mounts = MODULE.enumerate_roots([parent, child])

    assert "/data/0/p.txt" in paths and "/data/1/c.txt" in paths
    assert mapping["/data/1/c.txt"] == str(child.resolve() / "c.txt")
    assert [m.target for m in mounts] == ["/data/0", "/data/1"]
    assert [m.source for m in mounts] == [parent.resolve(), child.resolve()]


def test_enumerate_roots_single_unsafe_root_uses_plain_data(tmp_path):
    # When identity mounting is unsafe, a single root falls back to plain /data. Forcing
    # the decision keeps the test from having to enumerate a real "/" or "/usr".
    (tmp_path / "a.txt").write_text("x")
    with patch.object(MODULE, "_can_identity_mount", return_value=False):
        paths, mapping, mounts = MODULE.enumerate_roots([tmp_path])
    assert "/data/a.txt" in paths
    assert mapping["/data/a.txt"] == str(tmp_path.resolve() / "a.txt")
    assert mounts[0].target == "/data"


def test_enumerate_roots_fallback_namespaces_file_root(tmp_path):
    # The namespaced fallback mounts a file root at /data/<index>/<name> (not at /data/N).
    file_root = tmp_path / "solo.py"
    file_root.write_text("x")
    dir_root = tmp_path / "pkg"
    dir_root.mkdir()
    (dir_root / "a.py").write_text("x")
    with patch.object(MODULE, "_can_identity_mount", return_value=False):
        paths, mapping, mounts = MODULE.enumerate_roots([file_root, dir_root])
    assert "/data/0/solo.py" in paths and "/data/1/a.py" in paths
    assert mapping["/data/0/solo.py"] == str(file_root.resolve())
    assert [m.target for m in mounts] == ["/data/0/solo.py", "/data/1"]


def test_container_root_for_identity_uses_host_path_else_data(tmp_path):
    root = tmp_path.resolve()
    assert MODULE._container_root_for(root, identity=True, index=0, count=1) == str(root)
    assert MODULE._container_root_for(root, identity=True, index=1, count=2) == str(root)
    assert MODULE._container_root_for(root, identity=False, index=0, count=1) == "/data"
    assert MODULE._container_root_for(root, identity=False, index=1, count=3) == "/data/1"


@posix_only
def test_enumerate_roots_single_root_under_reserved_name_still_identity(tmp_path):
    # The reserved-name check only fires for a *direct child of /* (e.g. /usr); a deep
    # path that merely contains such a component is safe and stays identity-mounted.
    reserved_deep = tmp_path / "usr"
    reserved_deep.mkdir()
    (reserved_deep / "a.txt").write_text("x")
    paths, _, mounts = MODULE.enumerate_roots([reserved_deep])
    assert str(reserved_deep.resolve() / "a.txt") in paths
    assert mounts[0].target == str(reserved_deep.resolve())


# --- identity-mount safety predicates -------------------------------------------


@posix_only
def test_can_identity_mount_accepts_disjoint_nonreserved_roots():
    from pathlib import Path

    assert MODULE._can_identity_mount([Path("/Users/me/proj"), Path("/var/tmp/other")])


def test_can_identity_mount_rejects_filesystem_root():
    from pathlib import Path

    assert not MODULE._can_identity_mount([Path("/")])


def test_can_identity_mount_rejects_reserved_top_level_dirs():
    from pathlib import Path

    # Mounting at /usr or /etc would shadow the worker image's own system dirs.
    assert not MODULE._can_identity_mount([Path("/usr")])
    assert not MODULE._can_identity_mount([Path("/home"), Path("/srv/data")])


def test_can_identity_mount_rejects_overlapping_roots():
    from pathlib import Path

    assert not MODULE._can_identity_mount([Path("/a/b"), Path("/a/b/c")])
    assert not MODULE._can_identity_mount([Path("/a/b/c"), Path("/a/b")])


def test_can_identity_mount_rejects_empty():
    assert not MODULE._can_identity_mount([])


def test_normalize_roots_dedupes_and_requires_existing(tmp_path):
    (tmp_path / "x").write_text("x")
    roots = MODULE.normalize_roots([tmp_path, tmp_path])
    assert roots == [tmp_path.resolve()]
    with pytest.raises(FileNotFoundError):
        MODULE.normalize_roots([tmp_path / "missing"])


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


def test_cli_passes_multiple_paths_to_search():
    runner = CliRunner()
    with patch.object(cli.backend, "search", return_value=[]) as search:
        result = runner.invoke(cli.app, ["prompt", "/tmp", "/var"])
    assert result.exit_code == 0
    assert search.call_args.args[0] == ["/tmp", "/var"]


def test_cli_searches_given_directory():
    runner = CliRunner()
    with patch.object(cli.backend, "search", return_value=[]) as search:
        result = runner.invoke(cli.app, ["prompt", "."])
    assert result.exit_code == 0
    assert search.call_args.args[0] == ["."]


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


def test_cli_print0_conflicts_with_fields():
    runner = CliRunner()
    result = runner.invoke(cli.app, ["prompt", "/tmp", "--print0", "--fields"])
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


def test_cli_threads_sandbox_backend_to_search():
    result, search = _run(["--sandbox", "apple"])

    assert result.exit_code == 0
    assert search.call_args.kwargs["sandbox_backend"] == "apple"
    assert "Apple Containers sandbox is experimental" in result.output
    assert "does not disable networking on macOS 15" in result.output


def test_cli_apple_warning_mentions_network_none_on_macos_26():
    with patch.object(cli.sandbox_module, "apple_supports_no_network_flag", return_value=True):
        result, search = _run(["--sandbox", "apple"])

    assert result.exit_code == 0
    assert search.call_args.kwargs["sandbox_backend"] == "apple"
    assert "Apple Containers sandbox is experimental" in result.output
    assert "--network none" in result.output
    assert "does not disable networking on macOS 15" not in result.output


def test_cli_threads_sandbox_backend_to_run_saved(tmp_path):
    script = tmp_path / "f.py"
    script.write_text("def filter_paths(paths): return paths")
    runner = CliRunner()
    with patch.object(cli.backend, "run_saved", return_value=[]) as run_saved:
        result = runner.invoke(cli.app, ["--run", str(script), str(tmp_path), "--sandbox", "apple"])

    assert result.exit_code == 0
    assert run_saved.call_args.kwargs["sandbox_backend"] == "apple"
    assert "Apple Containers sandbox is experimental" in result.output


def test_cli_rejects_unknown_sandbox_backend():
    runner = CliRunner()
    result = runner.invoke(cli.app, ["prompt", "/tmp", "--sandbox", "gvisor"])

    assert result.exit_code == 2
    assert "--sandbox must be one of" in result.output


# --- reading the root list from stdin ('-') -------------------------------------


def test_cli_dash_reads_newline_paths_from_stdin():
    runner = CliRunner()
    with patch.object(cli.backend, "search", return_value=[]) as search:
        result = runner.invoke(cli.app, ["prompt", "-"], input="a.txt\nb.txt\n")
    assert result.exit_code == 0
    assert search.call_args.args[0] == ["a.txt", "b.txt"]


def test_cli_dash_reads_nul_delimited_paths_from_stdin():
    # NUL delimiters are auto-detected so '-' consumes `find -print0` / `nfind --print0`.
    runner = CliRunner()
    with patch.object(cli.backend, "search", return_value=[]) as search:
        result = runner.invoke(cli.app, ["prompt", "-"], input="a b.txt\0c.txt\0")
    assert result.exit_code == 0
    assert search.call_args.args[0] == ["a b.txt", "c.txt"]


def test_cli_dash_merges_with_explicit_paths():
    runner = CliRunner()
    with patch.object(cli.backend, "search", return_value=[]) as search:
        result = runner.invoke(cli.app, ["prompt", "explicit", "-"], input="from-stdin.txt\n")
    assert result.exit_code == 0
    assert search.call_args.args[0] == ["explicit", "from-stdin.txt"]


def test_cli_dash_empty_stdin_emits_nothing_and_skips_search():
    # Empty stdin must not fall back to searching '.'; it should run nothing.
    runner = CliRunner()
    with patch.object(cli.backend, "search", return_value=[]) as search:
        result = runner.invoke(cli.app, ["prompt", "-"], input="")
    assert result.exit_code == 0
    assert result.stdout == ""
    search.assert_not_called()


def test_cli_dash_with_run_reads_stdin(tmp_path):
    script = tmp_path / "f.py"
    script.write_text("def filter_paths(paths): return paths")
    runner = CliRunner()
    with patch.object(cli.backend, "run_saved", return_value=[]) as run_saved:
        result = runner.invoke(cli.app, ["--run", str(script), "-"], input="a.txt\nb.txt\n")
    assert result.exit_code == 0
    assert run_saved.call_args.args[1] == ["a.txt", "b.txt"]


def test_resolve_stdin_paths_passthrough_without_dash():
    request = GeneratedSearchRequest(prompt="p", paths=["a", "b"])
    resolved, no_paths = cli._resolve_stdin_paths(request)
    assert resolved is request and no_paths is False


def test_resolve_stdin_paths_errors_on_tty():
    request = GeneratedSearchRequest(prompt="p", paths=["-"])
    fake_stdin = Mock()
    fake_stdin.isatty.return_value = True
    with (
        patch.object(cli.sys, "stdin", fake_stdin),
        pytest.raises(ValueError, match="stdin is a terminal"),
    ):
        cli._resolve_stdin_paths(request)
