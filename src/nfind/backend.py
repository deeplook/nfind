#!/usr/bin/env python3
"""Search paths with an LLM-generated filter executed inside a sandbox backend.

The host enumerates the search tree and asks the model for code. The generated
code runs in a disposable container with the search root mounted at /data as
read-only. Only paths supplied by the host may be returned.

The in-container worker that runs the generated code lives in :mod:`nfind.worker`,
a self-contained, standard-library-only module the worker image ships and runs as
``python worker.py --worker``.
"""

from __future__ import annotations

import subprocess as subprocess
import sys as sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .constants import _RETRY_TEMPERATURE as _RETRY_TEMPERATURE
from .constants import DEFAULT_ALLOWED_PACKAGES as DEFAULT_ALLOWED_PACKAGES
from .constants import DEFAULT_BUILD_TIMEOUT as DEFAULT_BUILD_TIMEOUT
from .constants import DEFAULT_GENERATION_ATTEMPTS as DEFAULT_GENERATION_ATTEMPTS
from .constants import DEFAULT_IMAGE as DEFAULT_IMAGE
from .constants import DEFAULT_MODEL as DEFAULT_MODEL
from .constants import DEFAULT_NODE_IMAGE as DEFAULT_NODE_IMAGE
from .constants import DEFAULT_PROVIDER as DEFAULT_PROVIDER
from .constants import DEFAULT_RUNTIME as DEFAULT_RUNTIME
from .constants import DOCKER_CHECK_TIMEOUT as DOCKER_CHECK_TIMEOUT
from .constants import FILTER_LINE_LENGTH as FILTER_LINE_LENGTH
from .constants import PROVIDERS as PROVIDERS
from .endpoint_cache import get_endpoint as get_endpoint
from .endpoint_cache import set_endpoint as set_endpoint
from .enumeration import _matches_any as _matches_any
from .enumeration import _normalize_roots as _normalize_roots
from .enumeration import enumerate_paths as enumerate_paths
from .enumeration import enumerate_roots as enumerate_roots
from .errors import DependencyError as DependencyError
from .errors import DockerError as DockerError
from .errors import DockerUnavailableError as DockerUnavailableError
from .execution import _parse_worker_response as _parse_worker_response
from .execution import _run_generated as _run_generated
from .execution import build_worker_image as build_worker_image
from .execution import run_filter as run_filter
from .execution import run_generated as run_generated
from .generation import _MACOS_META_SYSTEM as _MACOS_META_SYSTEM
from .generation import _RETRY_TEMPLATE as _RETRY_TEMPLATE
from .generation import _SYSTEM as _SYSTEM
from .generation import _USER_TEMPLATE as _USER_TEMPLATE
from .generation import _adapt_request as _adapt_request
from .generation import _check_undefined_names as _check_undefined_names
from .generation import _create_response as _create_response
from .generation import _extract_json_object as _extract_json_object
from .generation import _format_generated_code as _format_generated_code
from .generation import _is_model_not_found as _is_model_not_found
from .generation import _is_responses_only as _is_responses_only
from .generation import _make_client as _make_client
from .generation import _parse_generation as _parse_generation
from .generation import _request_completion as _request_completion
from .generation import _ruff_path as _ruff_path
from .generation import _split_model as _split_model
from .generation import _strip_code_fence as _strip_code_fence
from .generation import generate_filter as generate_filter
from .generation import list_models as list_models
from .metadata import collect_macos_metadata as collect_macos_metadata
from .runtimes import NODE_RUNTIME as NODE_RUNTIME
from .runtimes import PYTHON_RUNTIME as PYTHON_RUNTIME
from .runtimes import RUNTIMES as RUNTIMES
from .runtimes import GeneratedFilter as GeneratedFilter
from .runtimes import _imply_packages as _imply_packages
from .runtimes import _validate_code_shape as _validate_code_shape
from .runtimes import _validate_dependencies as _validate_dependencies
from .sandbox import DEFAULT_SANDBOX_BACKEND as DEFAULT_SANDBOX_BACKEND
from .sandbox import DockerSandbox as DockerSandbox
from .sandbox import Limits as Limits
from .sandbox import Mount as Mount
from .sandbox import Sandbox as Sandbox
from .sandbox import SandboxBackend as SandboxBackend
from .sandbox import SandboxError as SandboxError
from .sandbox import SandboxOutputTooLarge as SandboxOutputTooLarge
from .sandbox import SandboxTimeout as SandboxTimeout
from .sandbox import _derived_image_tag as _derived_image_tag
from .sandbox import _dockerfile_path as _dockerfile_path
from .sandbox import _image_exists as _image_exists
from .sandbox import _remove_container as _remove_container
from .sandbox import _run_docker as _run_docker
from .sandbox import build_image as build_image
from .sandbox import check_docker_available as check_docker_available
from .sandbox import check_sandbox_available as check_sandbox_available
from .serialization import _SCRIPT_METADATA_RE as _SCRIPT_METADATA_RE
from .serialization import deserialize_filter as deserialize_filter
from .serialization import serialize_filter as serialize_filter
from .whitelist import _whitelist_path as _whitelist_path
from .whitelist import approve_packages as approve_packages
from .whitelist import load_whitelist as load_whitelist
from .worker import MAX_RESULT_BYTES as MAX_RESULT_BYTES
from .worker import _module_main as _module_main
from .worker import _normalize_results as _normalize_results
from .worker import _worker_response as _worker_response
from .worker import execute_worker_main as execute_worker_main
from .worker import worker_main as worker_main


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
    or a single file. With several roots each is mounted separately and its entries are
    namespaced so identically named files don't collide. ``exclude`` (glob patterns),
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
    generated = generate_filter(prompt, model=model, on_retry=on_retry, macos_meta=macos_meta)
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
