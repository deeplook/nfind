"""nfind -- natural-language file search backed by a sandboxed LLM filter."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .backend import (
    DependencyError,
    DockerError,
    DockerUnavailableError,
    load_whitelist,
    run_saved,
    search,
    serialize_filter,
)

try:
    # Single source of truth: the version declared in pyproject.toml, read from the
    # installed package metadata at runtime.
    __version__ = version("nfind")
except PackageNotFoundError:  # pragma: no cover - only when running from an uninstalled tree
    __version__ = "0.0.0+unknown"

__all__ = [
    "DependencyError",
    "DockerError",
    "DockerUnavailableError",
    "load_whitelist",
    "serialize_filter",
    "run_saved",
    "search",
    "__version__",
]
