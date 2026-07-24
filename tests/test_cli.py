"""Unit tests for the CLI that need no sandbox backend or LLM."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import nfind
from nfind import cli
from nfind.config import configurable_param_names, known_config_keys, load_config
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
def test_top_level_help_shows_search_help_and_subcommands(args: list[str], code: int) -> None:
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == code
    # The primary (search) command's help is shown -- its PROMPT argument, not a bare
    # group listing -- plus a Subcommands panel listing cache and config. Assert on short,
    # stable tokens only: Rich wraps/truncates long option names by the runner's terminal
    # width, so they are unreliable in rendered help.
    assert "PROMPT" in result.output
    assert "Subcommands" in result.output
    assert "cache" in result.output
    assert "config" in result.output


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


# -- config subcommand ------------------------------------------------------


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.toml"
    monkeypatch.setenv("NFIND_CONFIG", str(path))
    return path


def test_config_help_lists_verbs() -> None:
    result = CliRunner().invoke(cli.app, ["config", "-h"])
    assert result.exit_code == 0
    for verb in ("path", "show", "get", "edit"):
        assert verb in result.output


def test_config_init_writes_template(config_file: Path) -> None:
    assert not config_file.exists()
    result = CliRunner().invoke(cli.app, ["config", "init"])
    assert result.exit_code == 0
    text = config_file.read_text()
    # Header + every key present as a commented example, with real defaults interpolated.
    assert "nfind configuration file" in text
    assert 'model = "openai/gpt-5.4"' in text
    for key in known_config_keys():
        assert key in text
    # All lines are commented, so the fresh file overrides nothing.
    assert load_config(config_file) == {}


def test_config_init_refuses_to_overwrite(config_file: Path) -> None:
    config_file.write_text('model = "anthropic/claude-sonnet-4-6"\n')
    result = CliRunner().invoke(cli.app, ["config", "init"])
    assert result.exit_code == 1
    assert "already exists" in result.stderr
    # Existing file untouched.
    assert load_config(config_file) == {"model": "anthropic/claude-sonnet-4-6"}


def test_config_init_force_overwrites(config_file: Path) -> None:
    config_file.write_text('model = "anthropic/claude-sonnet-4-6"\n')
    result = CliRunner().invoke(cli.app, ["config", "init", "--force"])
    assert result.exit_code == 0
    assert load_config(config_file) == {}  # replaced by the all-commented template


def test_config_path_prints_resolved_path(config_file: Path) -> None:
    result = CliRunner().invoke(cli.app, ["config", "path"])
    assert result.exit_code == 0
    assert result.output.strip() == str(config_file)


def test_config_show_missing_notes_defaults(config_file: Path) -> None:
    result = CliRunner().invoke(cli.app, ["config", "show"])
    assert result.exit_code == 0
    assert "no config file" in result.stderr
    assert str(config_file) in result.stderr


def test_config_show_prints_file(config_file: Path) -> None:
    config_file.write_text('model = "openai/gpt-5.4"\ntimeout = 30\n')
    result = CliRunner().invoke(cli.app, ["config", "show"])
    assert result.exit_code == 0
    assert 'model = "openai/gpt-5.4"' in result.output
    assert "timeout = 30" in result.output


def test_config_get_scalar(config_file: Path) -> None:
    config_file.write_text('model = "anthropic/claude-sonnet-4-6"\n')
    result = CliRunner().invoke(cli.app, ["config", "get", "model"])
    assert result.exit_code == 0
    assert result.output.strip() == "anthropic/claude-sonnet-4-6"


def test_config_get_bool_renders_toml_style(config_file: Path) -> None:
    config_file.write_text("cache-semantic = true\n")
    result = CliRunner().invoke(cli.app, ["config", "get", "cache-semantic"])
    assert result.output.strip() == "true"


def test_config_get_accepts_underscore_spelling(config_file: Path) -> None:
    config_file.write_text("pids-limit = 128\n")
    result = CliRunner().invoke(cli.app, ["config", "get", "pids_limit"])
    assert result.output.strip() == "128"


def test_config_get_list_one_per_line(config_file: Path) -> None:
    config_file.write_text('exclude = ["vendor", "*.min.js"]\n')
    result = CliRunner().invoke(cli.app, ["config", "get", "exclude"])
    assert result.output.splitlines() == ["vendor", "*.min.js"]


def test_config_get_unset_key_notes_default(config_file: Path) -> None:
    config_file.write_text('model = "openai/gpt-5.4"\n')
    result = CliRunner().invoke(cli.app, ["config", "get", "timeout"])
    assert result.exit_code == 0
    # No value emitted, just an informational note on stderr.
    assert "unset" in result.stderr


def test_config_get_unknown_key_errors(config_file: Path) -> None:
    result = CliRunner().invoke(cli.app, ["config", "get", "bogus"])
    assert result.exit_code == 2
    assert "unknown config key" in result.stderr


def test_config_set_updates_value_and_preserves_comments(config_file: Path) -> None:
    config_file.write_text(
        "# Pick a strong model\n"
        'model = "openai/gpt-5.4"\n\n'
        "timeout = 30   # seconds the filter may run\n"
    )
    result = CliRunner().invoke(cli.app, ["config", "set", "timeout", "45"])
    assert result.exit_code == 0
    text = config_file.read_text()
    # The value changed...
    assert "45" in text
    assert "timeout = 30" not in text
    # ...but the comments survived (this is why we use tomlkit, not a dict round-trip).
    assert "# Pick a strong model" in text
    assert "# seconds the filter may run" in text


def test_config_set_list_key_consumes_all_values(config_file: Path) -> None:
    config_file.write_text("max-depth = 3\n")
    result = CliRunner().invoke(cli.app, ["config", "set", "exclude", "vendor", "dist", "*.min.js"])
    assert result.exit_code == 0
    assert load_config(config_file)["exclude"] == ["vendor", "dist", "*.min.js"]


def test_config_set_creates_file_when_absent(config_file: Path) -> None:
    assert not config_file.exists()
    result = CliRunner().invoke(cli.app, ["config", "set", "model", "anthropic/claude-sonnet-4-6"])
    assert result.exit_code == 0
    assert config_file.is_file()
    assert load_config(config_file)["model"] == "anthropic/claude-sonnet-4-6"


def test_config_set_updates_existing_underscore_spelling(config_file: Path) -> None:
    config_file.write_text("pids_limit = 64\n")
    result = CliRunner().invoke(cli.app, ["config", "set", "pids-limit", "128"])
    assert result.exit_code == 0
    text = config_file.read_text()
    assert "pids_limit = 128" in text  # updated in place, not duplicated
    assert text.count("pids") == 1


def test_config_set_roundtrips_through_load_config(config_file: Path) -> None:
    CliRunner().invoke(cli.app, ["config", "set", "cache-semantic", "true"])
    CliRunner().invoke(cli.app, ["config", "set", "cache-threshold", "0.2"])
    assert load_config(config_file) == {"semantic": True, "cache_threshold": 0.2}


@pytest.mark.parametrize(
    "args",
    [
        ["config", "set", "timeout", "abc"],  # not a number
        ["config", "set", "sandbox", "jail"],  # not a valid backend
        ["config", "set", "bogus", "1"],  # unknown key
        ["config", "set", "timeout", "30", "60"],  # scalar given two values
    ],
)
def test_config_set_rejects_bad_input(config_file: Path, args: list[str]) -> None:
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 2
    assert "error:" in result.stderr


def test_config_unset_removes_key(config_file: Path) -> None:
    config_file.write_text('model = "openai/gpt-5.4"\ntimeout = 30\n')
    result = CliRunner().invoke(cli.app, ["config", "unset", "timeout"])
    assert result.exit_code == 0
    assert load_config(config_file) == {"model": "openai/gpt-5.4"}


def test_config_unset_missing_key_is_noop(config_file: Path) -> None:
    config_file.write_text('model = "openai/gpt-5.4"\n')
    result = CliRunner().invoke(cli.app, ["config", "unset", "timeout"])
    assert result.exit_code == 0
    assert "not set" in result.stderr


def test_config_unset_no_file_is_noop(config_file: Path) -> None:
    result = CliRunner().invoke(cli.app, ["config", "unset", "timeout"])
    assert result.exit_code == 0
    assert "no config file" in result.stderr


def test_config_unset_unknown_key_errors(config_file: Path) -> None:
    result = CliRunner().invoke(cli.app, ["config", "unset", "bogus"])
    assert result.exit_code == 2
    assert "unknown config key" in result.stderr


def test_config_edit_creates_dir_and_opens_editor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "nested" / "config.toml"
    monkeypatch.setenv("NFIND_CONFIG", str(target))
    captured: dict[str, object] = {}
    monkeypatch.setattr("nfind.cli.click.edit", lambda **kwargs: captured.update(kwargs))
    result = CliRunner().invoke(cli.app, ["config", "edit"])
    assert result.exit_code == 0
    assert captured == {"filename": str(target)}
    assert target.parent.is_dir()  # parent created even though the file itself was not


# -- config-settable option marking ----------------------------------------


def _search_command_option_help() -> dict[str, str]:
    """Help text of every option on the built `search` command, with config marks applied."""
    import typer.main

    group: object = typer.main.get_command(cli.app)
    search = group.commands[cli._DEFAULT_COMMAND]  # type: ignore[attr-defined]
    cli._style_search_help(search)  # the marks/panels the group applies on resolution
    return {
        param.name: (getattr(param, "help", "") or "")
        for param in search.params
        if getattr(param, "param_type_name", None) == "option"
    }


def test_configurable_options_are_marked_and_others_are_not() -> None:
    opts = _search_command_option_help()
    marked = {name for name, help_text in opts.items() if help_text.endswith("(config)")}
    # Exactly the options a config key can set are marked -- no drift in either direction.
    assert marked == (configurable_param_names() & set(opts))


def test_show_code_is_configurable_and_marked() -> None:
    opts = _search_command_option_help()
    assert "show-code" in known_config_keys()
    assert opts["show_code"].endswith("(config)")


@pytest.mark.parametrize(
    "per_run_option",
    ["save", "run", "yes", "no_deps", "force", "confirm", "extract", "macos_meta", "rebuild"],
)
def test_per_run_options_are_not_marked(per_run_option: str) -> None:
    assert not _search_command_option_help()[per_run_option].endswith("(config)")


def test_options_are_grouped_into_help_panels() -> None:
    import typer.main

    group: object = typer.main.get_command(cli.app)
    search = group.commands[cli._DEFAULT_COMMAND]  # type: ignore[attr-defined]
    cli._style_search_help(search)
    panels = {
        param.name: getattr(param, "rich_help_panel", None)
        for param in search.params
        if getattr(param, "param_type_name", None) == "option"
    }
    assert panels["model"] == "Model"
    assert panels["timeout"] == "Sandbox & resources"
    assert panels["exclude"] == "Search scope"
    assert panels["as_json"] == "Output"
    assert panels["show_code"] == "Generated filter"
    assert panels["yes"] == "Dependencies"
    assert panels["cache"] == "Query cache"
    # Meta options stay in Typer's default "Options" panel.
    assert panels["config_file"] is None
    assert panels["version"] is None


def test_help_legend_explains_config_mark() -> None:
    result = CliRunner().invoke(cli.app, ["-h"])
    assert "Options tagged (config)" in result.output


def test_rendered_help_marks_options_end_to_end() -> None:
    # The marks are applied as the group resolves the search command, so the real
    # `--help` render (legend + many option marks) carries them.
    result = CliRunner().invoke(cli.app, ["-h"])
    assert result.output.count("(config)") > 5


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
