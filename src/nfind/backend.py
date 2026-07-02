#!/usr/bin/env python3
"""Search paths with an LLM-generated filter executed inside a sandbox backend.

The host enumerates the search tree and asks the model for code. The generated
code runs in a disposable container with the search root bind-mounted read-only
(at its own host path when safe, else a neutral mountpoint). Only paths supplied
by the host may be returned.

The in-container worker that runs the generated code lives in :mod:`nfind.worker`,
a self-contained, standard-library-only module the worker image ships and runs as
``python worker.py --worker``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .constants import DEFAULT_BUILD_TIMEOUT, DEFAULT_MODEL
from .enumeration import _normalize_roots, enumerate_roots
from .errors import DependencyError, DockerError, DockerUnavailableError
from .execution import _run_generated
from .generation import _format_generated_code, generate_filter, list_models
from .metadata import collect_macos_metadata
from .runtimes import GeneratedFilter, _imply_packages
from .sandbox import (
    DEFAULT_SANDBOX_BACKEND,
    Sandbox,
    SandboxBackend,
    check_sandbox_available,
)
from .serialization import deserialize_filter, serialize_filter
from .whitelist import load_whitelist

# ``nfind.backend`` is the package's public entry point: it defines the top-level
# operations (``search``, ``generate_only``, ``run_saved``) and re-exports the
# handful of names that ``nfind`` and the CLI import through it. ``__all__`` lists
# exactly that public surface, which satisfies mypy's strict ``no_implicit_reexport``
# and ruff's unused-import check without per-import ``x as x`` aliases. Internal
# helpers live in (and are imported/tested from) their own submodules.
__all__ = [
    # Entry points defined here.
    "search",
    "generate_only",
    "run_saved",
    # Names re-exported for callers of ``nfind`` / ``nfind.backend``.
    "DEFAULT_BUILD_TIMEOUT",
    "DEFAULT_MODEL",
    "DEFAULT_SANDBOX_BACKEND",
    "DependencyError",
    "DockerError",
    "DockerUnavailableError",
    "GeneratedFilter",
    "SandboxBackend",
    "list_models",
    "load_whitelist",
    "serialize_filter",
]


def search(
    path: str | Path | Sequence[str | Path],
    prompt: str,
    *,
    image: str | None = None,
    model: str = DEFAULT_MODEL,
    timeout: float = 10.0,
    memory: str = "256m",
    cpus: float = 1.0,
    pids_limit: int = 64,
    rebuild: bool = False,
    build_timeout: float = DEFAULT_BUILD_TIMEOUT,
    on_generated: Callable[[GeneratedFilter], None] | None = None,
    on_retry: Callable[[int, ValueError], None] | None = None,
    approve_dependencies: Callable[[list[str]], bool] | None = None,
    whitelist: set[str] | None = None,
    macos_meta: bool = False,
    extract: bool = False,
    format_code: bool = True,
    sandbox: Sandbox | None = None,
    sandbox_backend: SandboxBackend = DEFAULT_SANDBOX_BACKEND,
    exclude: Sequence[str] = (),
    max_depth: int | None = None,
    use_default_ignores: bool = True,
) -> list[dict[str, Any]]:
    """Generate and execute a filter, returning host-path result records.

    Each record is a dict with at least a "path" key (a host path); the filter may
    attach extra per-path fields when the prompt asks for them.

    The model chooses the runtime (Python or Node.js); the matching base image is
    used unless ``image`` overrides the base tag.

    When ``format_code`` is true (the default), the generated Python filter is tidied
    with ruff -- unused imports removed, imports sorted, and the source reformatted --
    before it is shown, saved, or run. The transforms preserve behaviour and fall back
    to the original code on any failure.

    ``on_generated``, if given, is called with the ``GeneratedFilter`` after it is
    produced but before it runs. It may inspect, save, or display the code; raising
    from it (e.g. on a declined confirmation) aborts before execution.

    If the filter requests third-party packages that are not already approved
    (``whitelist``, defaulting to the runtime's built-in plus saved whitelist),
    ``approve_dependencies`` is called with the new package names. When it returns
    True the packages are installed into a derived image and remembered; otherwise
    a ``DependencyError`` is raised. Without an approver, unapproved packages are
    rejected.

    When ``macos_meta`` is true and the host is macOS, selected per-path attributes
    (Finder tags, quarantine/where-from) are read on the host and exposed to a Python
    filter as a global ``META`` dict, enabling queries that combine macOS metadata with
    file contents. It is a no-op on other platforms.

    ``sandbox`` overrides the execution backend (a :class:`~nfind.sandbox.DockerSandbox`
    built from the chosen runtime by default); pass a fake to run without Docker.

    ``path`` is a single root or a sequence of roots; each root may be a directory (walked)
    or a single file. Each root is bind-mounted at its own host path when that is safe;
    otherwise (a root is ``/``, would shadow a container system directory, or roots overlap)
    entries fall back to neutral ``/data`` mountpoints, namespaced so identically named
    files don't collide. ``exclude`` (glob patterns),
    ``use_default_ignores`` (skip common
    VCS/dependency/cache directories), and ``max_depth`` (limit traversal depth) shape
    which paths are enumerated and handed to the filter; see :func:`enumerate_roots`.
    """
    roots = _normalize_roots(path)
    container_paths, host_by_container, mounts = enumerate_roots(
        roots, exclude=exclude, max_depth=max_depth, use_default_ignores=use_default_ignores
    )
    if not container_paths:
        return []
    # Verify the selected sandbox up front so a missing backend fails before any API call.
    if sandbox is not None:
        sandbox.check_available()
    else:
        check_sandbox_available(sandbox_backend)
    meta = collect_macos_metadata(host_by_container) if macos_meta else {}
    generated = generate_filter(
        prompt, model=model, on_retry=on_retry, macos_meta=macos_meta, extract=extract
    )
    generated.dependencies = _imply_packages(generated.runtime, generated.dependencies)
    if format_code:
        generated.code = _format_generated_code(generated.code, generated.runtime)
    if on_generated is not None:
        on_generated(generated)

    return _run_generated(
        generated,
        mounts,
        container_paths,
        host_by_container,
        meta=meta,
        image=image,
        timeout=timeout,
        memory=memory,
        cpus=cpus,
        pids_limit=pids_limit,
        rebuild=rebuild,
        build_timeout=build_timeout,
        approve_dependencies=approve_dependencies,
        whitelist=whitelist,
        sandbox=sandbox,
        sandbox_backend=sandbox_backend,
    )


def generate_only(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    on_generated: Callable[[GeneratedFilter], None] | None = None,
    on_retry: Callable[[int, ValueError], None] | None = None,
    macos_meta: bool = False,
    extract: bool = False,
    format_code: bool = True,
) -> GeneratedFilter:
    """Generate a filter without running the sandbox.

    Makes the LLM call, applies ruff formatting when ``format_code`` is true,
    and calls ``on_generated`` if provided. Returns the ``GeneratedFilter``.

    Use this (or ``--no-exec`` on the CLI) when you want to inspect or save a
    filter without executing it — no path enumeration, no sandbox startup.
    """
    generated = generate_filter(
        prompt, model=model, on_retry=on_retry, macos_meta=macos_meta, extract=extract
    )
    generated.dependencies = _imply_packages(generated.runtime, generated.dependencies)
    if format_code:
        generated.code = _format_generated_code(generated.code, generated.runtime)
    if on_generated is not None:
        on_generated(generated)
    return generated


def run_saved(
    filter_path: str | Path,
    path: str | Path | Sequence[str | Path] = ".",
    *,
    image: str | None = None,
    timeout: float = 10.0,
    memory: str = "256m",
    cpus: float = 1.0,
    pids_limit: int = 64,
    rebuild: bool = False,
    build_timeout: float = DEFAULT_BUILD_TIMEOUT,
    approve_dependencies: Callable[[list[str]], bool] | None = None,
    whitelist: set[str] | None = None,
    on_generated: Callable[[GeneratedFilter], None] | None = None,
    sandbox: Sandbox | None = None,
    sandbox_backend: SandboxBackend = DEFAULT_SANDBOX_BACKEND,
    exclude: Sequence[str] = (),
    max_depth: int | None = None,
    use_default_ignores: bool = True,
) -> list[dict[str, Any]]:
    """Replay a previously saved filter through the sandbox, skipping the LLM.

    The file written by ``--save``/:func:`serialize_filter` is parsed back into a
    filter and run in the same hardened container as :func:`search`. Any third-party
    packages it declares are still gated through ``approve_dependencies``/the whitelist,
    so a saved filter cannot silently pull new packages. macOS metadata is not exposed
    on the replay path. ``path`` may be one root or several, each a directory or a file
    (mounted and namespaced as in :func:`search`); ``exclude``/``use_default_ignores``/
    ``max_depth`` shape enumeration exactly as for :func:`search`.
    """
    saved = Path(filter_path).expanduser()
    generated = deserialize_filter(saved.read_text(), filename=saved.name)
    if on_generated is not None:
        on_generated(generated)

    roots = _normalize_roots(path)
    container_paths, host_by_container, mounts = enumerate_roots(
        roots, exclude=exclude, max_depth=max_depth, use_default_ignores=use_default_ignores
    )
    if not container_paths:
        return []
    if sandbox is not None:
        sandbox.check_available()
    else:
        check_sandbox_available(sandbox_backend)
    return _run_generated(
        generated,
        mounts,
        container_paths,
        host_by_container,
        meta={},
        image=image,
        timeout=timeout,
        memory=memory,
        cpus=cpus,
        pids_limit=pids_limit,
        rebuild=rebuild,
        build_timeout=build_timeout,
        approve_dependencies=approve_dependencies,
        whitelist=whitelist,
        sandbox=sandbox,
        sandbox_backend=sandbox_backend,
    )
