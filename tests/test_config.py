import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from nfind import cli, config, constants

# --- config.load_config ---------------------------------------------------------


def test_load_config_reads_known_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'model = "anthropic/claude-3-5-sonnet-latest"\n'
        "timeout = 30\n"
        'memory = "512m"\n'
        "pids-limit = 128\n"
        "no-format = true\n"
        'sandbox = "podman"\n'
    )
    assert config.load_config(path) == {
        "model": "anthropic/claude-3-5-sonnet-latest",
        "timeout": 30.0,
        "memory": "512m",
        "pids_limit": 128,
        "no_format": True,
        "sandbox_backend": "podman",
    }


def test_load_config_rejects_unknown_sandbox_backend(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('sandbox = "jail"\n')
    with pytest.raises(config.ConfigError, match="expected one of"):
        config.load_config(path)


def test_load_config_accepts_underscore_key_spelling(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("pids_limit = 64\nbuild_timeout = 90\n")
    assert config.load_config(path) == {"pids_limit": 64, "build_timeout": 90.0}


def test_load_config_reads_enumeration_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'exclude = ["*.log", "build"]\nmax-depth = 3\nno-ignore = true\nprint0 = true\n'
    )
    assert config.load_config(path) == {
        "exclude": ["*.log", "build"],
        "max_depth": 3,
        "no_ignore": True,
        "print0": True,
    }


def test_load_config_reads_limit_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "command-timeout = 300\nmax-results = 100\nmax-items = 200\nmax-output-bytes = 4096\n"
    )
    assert config.load_config(path) == {
        "command_timeout": 300.0,
        "max_results": 100,
        "max_items": 200,
        "max_output_bytes": 4096,
    }


def test_load_config_reads_cache_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "cache = false\n"
        "cache-semantic = true\n"
        'cache-embedding-model = "ollama/nomic-embed-text"\n'
        "cache-threshold = 0.2\n"
    )
    assert config.load_config(path) == {
        "cache": False,
        "semantic": True,
        "cache_embedding_model": "ollama/nomic-embed-text",
        "cache_threshold": 0.2,
    }


def test_resolved_config_path_prefers_env(monkeypatch, tmp_path):
    target = tmp_path / "custom.toml"
    monkeypatch.setenv("NFIND_CONFIG", str(target))
    assert config.resolved_config_path() == target


def test_resolved_config_path_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("NFIND_CONFIG", raising=False)
    assert config.resolved_config_path() == config.default_config_path()


def test_known_config_keys_are_sorted_and_include_cache_keys():
    keys = config.known_config_keys()
    assert keys == sorted(keys)
    assert {"model", "cache", "cache-semantic", "cache-threshold"} <= set(keys)


@pytest.mark.parametrize(
    "key,raw,expected",
    [
        ("timeout", ["45"], 45.0),
        ("pids-limit", ["128"], 128),
        ("cache-semantic", ["true"], True),
        ("cache-semantic", ["off"], False),
        ("model", ["anthropic/claude-sonnet-4-6"], "anthropic/claude-sonnet-4-6"),
        ("sandbox", ["podman"], "podman"),
        ("exclude", ["vendor", "dist"], ["vendor", "dist"]),
    ],
)
def test_parse_config_value_valid(key, raw, expected):
    assert config.parse_config_value(key, raw) == expected


@pytest.mark.parametrize(
    "key,raw",
    [
        ("timeout", ["abc"]),  # not a number
        ("pids-limit", ["1.5"]),  # not an integer
        ("cache-semantic", ["maybe"]),  # not a bool
        ("sandbox", ["jail"]),  # not a valid backend
        ("bogus", ["1"]),  # unknown key
        ("timeout", ["1", "2"]),  # scalar given two values
        ("exclude", []),  # list given no values
    ],
)
def test_parse_config_value_invalid(key, raw):
    with pytest.raises(config.ConfigError):
        config.parse_config_value(key, raw)


def test_load_config_show_code_key(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("show-code = true\n")
    assert config.load_config(path) == {"show_code": True}


def test_configurable_param_names_includes_show_code():
    assert "show_code" in config.configurable_param_names()


def test_normalize_config_key_rejects_unknown():
    with pytest.raises(config.ConfigError):
        config.normalize_config_key("nope")
    assert config.normalize_config_key("pids_limit") == "pids-limit"


def test_documented_limit_defaults_match_constants():
    limits = (Path(__file__).parents[1] / "docs" / "limits.md").read_text()
    assert f"| Image build time | {constants.DEFAULT_BUILD_TIMEOUT:g} seconds |" in limits
    assert f"| Filter execution time | {constants.DEFAULT_TIMEOUT:g} seconds |" in limits
    memory_mb = constants.DEFAULT_MEMORY.removesuffix("m")
    assert f"| Worker memory | {memory_mb} MB |" in limits
    assert f"| Worker CPU | {constants.DEFAULT_CPUS:g} CPU |" in limits
    assert f"| Worker processes | {constants.DEFAULT_PIDS_LIMIT} |" in limits


def test_load_config_rejects_non_list_exclude(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('exclude = "*.log"\n')
    with pytest.raises(config.ConfigError, match="expected a list of strings"):
        config.load_config(path)


def test_load_config_rejects_unknown_key(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('modle = "x"\n')
    with pytest.raises(config.ConfigError, match="unknown config key 'modle'"):
        config.load_config(path)


def test_load_config_rejects_wrong_type(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('timeout = "fast"\n')
    with pytest.raises(config.ConfigError, match="timeout.*expected a number"):
        config.load_config(path)


def test_load_config_rejects_bool_for_numeric_key(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("pids-limit = true\n")
    with pytest.raises(config.ConfigError, match="expected an integer"):
        config.load_config(path)


def test_load_config_rejects_invalid_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("not = = toml")
    with pytest.raises(config.ConfigError, match="invalid TOML"):
        config.load_config(path)


def test_default_config_path_uses_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config.default_config_path() == tmp_path / "xdg" / "nfind" / "config.toml"


@pytest.mark.skipif(
    sys.platform == "win32", reason="~/.config fallback is Unix-only; Windows uses APPDATA"
)
def test_default_config_path_falls_back_to_home(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))
    assert config.default_config_path() == tmp_path / ".config" / "nfind" / "config.toml"


# --- CLI integration: precedence and errors -------------------------------------


def test_cli_applies_config_defaults(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('model = "anthropic/claude-x"\ntimeout = 42\nmemory = "1g"\n')
    runner = CliRunner()
    with patch.object(cli.backend, "search", return_value=[]) as search:
        result = runner.invoke(cli.app, ["files", str(tmp_path), "--config", str(cfg)])

    assert result.exit_code == 0
    assert search.call_args.kwargs["model"] == "anthropic/claude-x"
    assert search.call_args.kwargs["timeout"] == 42.0
    assert search.call_args.kwargs["memory"] == "1g"


def test_cli_flag_overrides_config(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("timeout = 42\n")
    runner = CliRunner()
    with patch.object(cli.backend, "search", return_value=[]) as search:
        result = runner.invoke(
            cli.app, ["files", str(tmp_path), "--config", str(cfg), "--timeout", "5"]
        )

    assert result.exit_code == 0
    assert search.call_args.kwargs["timeout"] == 5.0


def test_cli_reads_config_from_env_var(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text('memory = "2g"\n')
    monkeypatch.setenv("NFIND_CONFIG", str(cfg))
    runner = CliRunner()
    with patch.object(cli.backend, "search", return_value=[]) as search:
        result = runner.invoke(cli.app, ["files", str(tmp_path)])

    assert result.exit_code == 0
    assert search.call_args.kwargs["memory"] == "2g"


def test_cli_reads_default_config_path(tmp_path, monkeypatch):
    # With neither --config nor NFIND_CONFIG, the XDG default location is used if present.
    cfg_dir = tmp_path / "xdg" / "nfind"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text("fields = true\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    runner = CliRunner()
    with patch.object(cli.backend, "search", return_value=[{"path": "/a", "lines": 3}]):
        result = runner.invoke(cli.app, ["files", str(tmp_path)])

    assert result.exit_code == 0
    assert "/a\tlines=3" in result.output  # fields default came from the config file


def test_cli_missing_explicit_config_errors(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["files", str(tmp_path), "--config", str(tmp_path / "nope.toml")]
    )
    assert result.exit_code == 2
    # Rich wraps the error into a bordered panel, so match on a stable fragment.
    assert "nope.toml" in result.output


def test_cli_unknown_config_key_errors(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("bogus = 1\n")
    runner = CliRunner()
    result = runner.invoke(cli.app, ["files", str(tmp_path), "--config", str(cfg)])
    assert result.exit_code == 2
    assert "unknown config key" in result.output


@pytest.mark.skipif(not hasattr(__import__("signal"), "setitimer"), reason="POSIX timer required")
def test_cli_whole_command_timeout():
    def slow_models(_model):
        time.sleep(0.2)
        return []

    runner = CliRunner()
    with patch.object(cli.backend, "list_models", side_effect=slow_models):
        result = runner.invoke(
            cli.app,
            ["--list-models", "--command-timeout", "0.01"],
        )

    assert result.exit_code == 1
    assert "whole-command timeout" in result.output
