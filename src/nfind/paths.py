"""Cross-platform locations for nfind's per-user config and cache directories.

nfind is a CLI for developers, so on Linux and macOS it follows the Unix/XDG
convention (``$XDG_CONFIG_HOME``/``$XDG_CACHE_HOME``, falling back to the ``~/.config``
and ``~/.cache`` dotfolders that CLI tools use on macOS rather than ``~/Library``). On
Windows, where there is no dotfile convention, it uses the native app-data roots:
``%APPDATA%`` for config and ``%LOCALAPPDATA%`` for cache.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_WINDOWS_BASES = {"config": ("APPDATA", "Roaming"), "cache": ("LOCALAPPDATA", "Local")}


def user_dir(kind: str) -> Path:
    """Return nfind's per-user directory for ``kind`` (``"config"`` or ``"cache"``).

    The directory is not created; callers create it when they write.
    """
    if kind not in _WINDOWS_BASES:
        raise ValueError(f"unknown user directory kind: {kind!r}")
    if sys.platform == "win32":
        env_var, subdir = _WINDOWS_BASES[kind]
        base = os.environ.get(env_var) or str(Path.home() / "AppData" / subdir)
    else:
        base = os.environ.get(f"XDG_{kind.upper()}_HOME") or str(Path.home() / f".{kind}")
    return Path(base) / "nfind"
