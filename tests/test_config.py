import sys
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from nfind import cli, config

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
