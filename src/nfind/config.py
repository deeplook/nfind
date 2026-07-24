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

import os
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from .paths import user_dir
from .sandbox import SANDBOX_BACKENDS, SandboxBackend


class ConfigError(Exception):
    """Raised when a config file cannot be read, parsed, or validated."""


def _as_str(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError("expected a string")
    return value


def _as_sandbox_backend(value: Any) -> SandboxBackend:
    backend = _as_str(value)
    if backend not in SANDBOX_BACKENDS:
        choices = ", ".join(SANDBOX_BACKENDS)
        raise ConfigError(f"expected one of: {choices}")
    return cast(SandboxBackend, backend)


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
    "command-timeout": ("command_timeout", _as_float),
    "memory": ("memory", _as_str),
    "cpus": ("cpus", _as_float),
    "pids-limit": ("pids_limit", _as_int),
    "build-timeout": ("build_timeout", _as_float),
    "json": ("as_json", _as_bool),
    "fields": ("fields", _as_bool),
    "show-code": ("show_code", _as_bool),
    "no-format": ("no_format", _as_bool),
    "exclude": ("exclude", _as_str_list),
    "no-ignore": ("no_ignore", _as_bool),
    "max-depth": ("max_depth", _as_int),
    "print0": ("print0", _as_bool),
    "max-results": ("max_results", _as_int),
    "max-items": ("max_items", _as_int),
    "max-output-bytes": ("max_output_bytes", _as_int),
    "cache": ("cache", _as_bool),
    "cache-semantic": ("semantic", _as_bool),
    "cache-embedding-model": ("cache_embedding_model", _as_str),
    "cache-threshold": ("cache_threshold", _as_float),
}


def default_config_path() -> Path:
    """Location of the config file when neither --config nor NFIND_CONFIG is set."""
    return user_dir("config") / "config.toml"


def resolved_config_path() -> Path:
    """The config file nfind would read: ``$NFIND_CONFIG`` if set, else the default location.

    Mirrors the CLI's ``--config``/``NFIND_CONFIG`` resolution (minus the per-invocation
    ``--config`` flag) so ``nfind config`` reports and edits the same file a search reads.
    """
    override = os.environ.get("NFIND_CONFIG")
    if override:
        return Path(override).expanduser()
    return default_config_path()


def known_config_keys() -> list[str]:
    """The config-file keys nfind understands, in option-flag spelling, sorted."""
    return sorted(_SCHEMA)


def configurable_param_names() -> set[str]:
    """The CLI/click parameter names that a config-file key can supply a default for.

    Used to mark the matching options in ``--help``. Some keys (the semantic-cache ones)
    map to parameters that are not CLI options; those simply match nothing.
    """
    return {param for param, _ in _SCHEMA.values()}


def normalize_config_key(key: str) -> str:
    """Return the canonical (hyphen-spelled) form of ``key`` or raise for an unknown one."""
    normalized = key.replace("_", "-")
    if normalized not in _SCHEMA:
        valid = ", ".join(sorted(_SCHEMA))
        raise ConfigError(f"unknown config key {key!r}. Valid keys: {valid}")
    return normalized


def _parse_bool(text: str) -> bool:
    low = text.strip().lower()
    if low in {"true", "1", "yes", "on"}:
        return True
    if low in {"false", "0", "no", "off"}:
        return False
    raise ConfigError("expected true or false")


def _parse_int(text: str) -> int:
    try:
        return int(text)
    except ValueError as exc:
        raise ConfigError("expected an integer") from exc


def _parse_float(text: str) -> float:
    try:
        return float(text)
    except ValueError as exc:
        raise ConfigError("expected a number") from exc


def parse_config_value(key: str, raw_values: list[str]) -> Any:
    """Parse CLI string value(s) for ``key`` into a validated TOML-writable Python value.

    The target type is inferred from the key's schema entry: list keys (e.g. ``exclude``)
    consume every value, scalar keys take exactly one. The parsed value is then run through
    the same coercion :func:`load_config` uses, so ``config set`` rejects bad input with the
    identical error a bad file would produce.
    """
    normalized = normalize_config_key(key)
    _, coerce = _SCHEMA[normalized]
    if coerce is _as_str_list:
        if not raw_values:
            raise ConfigError(f"config key {normalized!r} needs at least one value")
        return coerce(list(raw_values))
    if len(raw_values) != 1:
        raise ConfigError(f"config key {normalized!r} takes a single value")
    text = raw_values[0]
    typed: Any
    if coerce is _as_int:
        typed = _parse_int(text)
    elif coerce is _as_float:
        typed = _parse_float(text)
    elif coerce is _as_bool:
        typed = _parse_bool(text)
    else:  # string-valued keys (including the validated sandbox backend)
        typed = text
    try:
        return coerce(typed)
    except ConfigError as exc:
        raise ConfigError(f"config key {normalized!r}: {exc}") from exc


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
