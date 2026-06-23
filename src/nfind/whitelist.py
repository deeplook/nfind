"""Persistence of user-approved packages (the dependency whitelist).

The effective allow-set for a runtime is its built-in defaults plus the packages the
user has approved, stored as JSON in nfind's config directory as ``whitelist.json``
(or ``$NFIND_WHITELIST`` when set). See :mod:`nfind.paths` for the per-OS location.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .constants import DEFAULT_RUNTIME
from .paths import user_dir
from .runtimes import RUNTIMES


def _whitelist_path() -> Path:
    """Location of the persisted package whitelist."""
    override = os.environ.get("NFIND_WHITELIST")
    if override:
        return Path(override)
    return user_dir("config") / "whitelist.json"


def _read_whitelist_file() -> dict[str, Any]:
    path = _whitelist_path()
    if path.exists():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return data
    return {}


def _saved_packages(data: dict[str, Any], runtime: str) -> set[str]:
    # Canonicalize on read so older files with non-normalized names (e.g. an underscore
    # variant alongside its dash form) collapse to one entry and match the defaults.
    normalize = RUNTIMES[runtime].normalize_name
    saved = {normalize(p) for p in data.get(runtime, []) if isinstance(p, str)}
    if runtime == DEFAULT_RUNTIME:
        # Absorb the pre-runtime flat format, {"packages": [...]}, as Python.
        saved |= {normalize(p) for p in data.get("packages", []) if isinstance(p, str)}
    return saved


def load_whitelist(runtime: str = DEFAULT_RUNTIME) -> set[str]:
    """Return approved package names for a runtime: defaults plus saved approvals."""
    defaults = set(RUNTIMES[runtime].default_packages)
    return defaults | _saved_packages(_read_whitelist_file(), runtime)


def approve_packages(packages: Sequence[str], runtime: str = DEFAULT_RUNTIME) -> None:
    """Persist newly approved packages for a runtime to the user's whitelist file."""
    if not packages:
        return
    data = _read_whitelist_file()
    normalize = RUNTIMES[runtime].normalize_name
    existing = _saved_packages(data, runtime)
    existing |= {normalize(p) for p in packages}
    data[runtime] = sorted(existing)
    data.pop("packages", None)  # migrate away from the legacy flat format
    path = _whitelist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
