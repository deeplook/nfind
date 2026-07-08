"""nfind -- natural-language file search backed by a sandboxed LLM filter."""

from __future__ import annotations

from .backend import (
    DependencyError,
    DockerError,
    DockerUnavailableError,
    load_whitelist,
    run_saved,
    search,
    serialize_filter,
)

__version__ = "0.2.0"

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
