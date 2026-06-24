"""Optional TOML config file supplying defaults for CLI options.

nfind needs no config file to run; when one is present it only provides *defaults* for a
subset of the command-line options, so the precedence is

    command-line option  >  --config / NFIND_CONFIG file  >  built-in default

The file is looked up at ``--config``/``$NFIND_CONFIG`` if given, otherwise at
``config.toml`` in nfind's config directory (see :mod:`nfind.paths` for the per-OS
location) and used only when it exists. Keys mirror the option flag names
(``pids-limit``); the underscore spelling (``pids_limit``) is accepted too.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast

from .paths import user_dir


class ConfigError(Exception):
    """Raised when a config file cannot be read, parsed, or validated."""


def _as_str(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError("expected a string")
    return value


def _as_sandbox_backend(value: Any) -> Literal["docker", "apple"]:
    backend = _as_str(value)
    if backend not in {"docker", "apple"}:
        raise ConfigError("expected one of: docker, apple")
    return cast(Literal["docker", "apple"], backend)


def _as_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ConfigError("expected true or false")
    return value


def _as_int(value: Any) -> int:
    # bool is a subclass of int; reject it so `pids-limit = true` is an error.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError("expected an integer")
    return value


def _as_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("expected a number")
    return float(value)


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError("expected a list of strings")
    return value


# Config key (option flag name) -> (CLI/click parameter name, coercion). Only options that
# represent reusable defaults and are valid in both search and --run modes are included;
# per-invocation actions (--save/--run) and approval shortcuts (--yes/--no-deps) are not.
_SCHEMA: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "model": ("model", _as_str),
    "sandbox": ("sandbox_backend", _as_sandbox_backend),
    "image": ("image", _as_str),
    "timeout": ("timeout", _as_float),
    "memory": ("memory", _as_str),
    "cpus": ("cpus", _as_float),
    "pids-limit": ("pids_limit", _as_int),
    "build-timeout": ("build_timeout", _as_float),
    "json": ("as_json", _as_bool),
    "verbose": ("verbose", _as_bool),
    "no-format": ("no_format", _as_bool),
    "exclude": ("exclude", _as_str_list),
    "no-ignore": ("no_ignore", _as_bool),
    "max-depth": ("max_depth", _as_int),
    "print0": ("print0", _as_bool),
}


def default_config_path() -> Path:
    """Location of the config file when neither --config nor NFIND_CONFIG is set."""
    return user_dir("config") / "config.toml"


def load_config(path: Path) -> dict[str, Any]:
    """Read and validate a TOML config file into a ``{parameter_name: value}`` dict.

    Keys may use the option flag spelling (``pids-limit``) or underscores
    (``pids_limit``). Unknown keys and wrong value types raise :class:`ConfigError`.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"could not read config file {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in config file {path}: {exc}") from exc

    defaults: dict[str, Any] = {}
    for raw_key, value in data.items():
        key = raw_key.replace("_", "-")
        if key not in _SCHEMA:
            valid = ", ".join(sorted(_SCHEMA))
            raise ConfigError(f"unknown config key {raw_key!r} in {path}. Valid keys: {valid}")
        param, coerce = _SCHEMA[key]
        try:
            defaults[param] = coerce(value)
        except ConfigError as exc:
            raise ConfigError(f"config key {raw_key!r} in {path}: {exc}") from exc
    return defaults
