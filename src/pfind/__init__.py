"""pfind -- natural-language file search backed by a sandboxed LLM filter."""

from __future__ import annotations

from .backend import (
    DependencyError,
    DockerError,
    DockerUnavailableError,
    load_whitelist,
    render_saved_filter,
    run_saved,
    search,
)

__version__ = "0.1.0"

__all__ = [
    "DependencyError",
    "DockerError",
    "DockerUnavailableError",
    "load_whitelist",
    "render_saved_filter",
    "run_saved",
    "search",
    "__version__",
]
