#!/usr/bin/env python3
"""Search paths with an LLM-generated Python filter executed inside Docker.

The host enumerates the search tree and asks the model for code.  The generated
code runs in a disposable container with the search root mounted at /data as
read-only.  Only paths supplied by the host may be returned.

The in-container worker that runs the generated code lives in :mod:`nfind.worker`,
a self-contained, standard-library-only module the Docker image ships and runs as
``python worker.py --worker``.
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess as subprocess
import sys as sys
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from .constants import _RETRY_TEMPERATURE as _RETRY_TEMPERATURE
from .constants import DEFAULT_ALLOWED_PACKAGES as DEFAULT_ALLOWED_PACKAGES
from .constants import DEFAULT_BUILD_TIMEOUT as DEFAULT_BUILD_TIMEOUT
from .constants import DEFAULT_GENERATION_ATTEMPTS as DEFAULT_GENERATION_ATTEMPTS
from .constants import DEFAULT_IGNORES as DEFAULT_IGNORES
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
from .errors import DependencyError as DependencyError
from .errors import DockerError as DockerError
from .errors import DockerUnavailableError as DockerUnavailableError
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
from .runtimes import Runtime as Runtime
from .runtimes import _imply_packages as _imply_packages
from .runtimes import _validate_code_shape as _validate_code_shape
from .runtimes import _validate_dependencies as _validate_dependencies
from .sandbox import DockerSandbox as DockerSandbox
from .sandbox import Limits as Limits
from .sandbox import Mount as Mount
from .sandbox import Sandbox as Sandbox
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


def _matches_any(name: str, relative_posix: str, patterns: Sequence[str]) -> bool:
    """True when ``name`` or its root-relative POSIX path matches any glob in ``patterns``."""
    return any(
        fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relative_posix, pattern)
        for pattern in patterns
    )


def enumerate_paths(
    search_root: Path,
    *,
    exclude: Sequence[str] = (),
    max_depth: int | None = None,
    use_default_ignores: bool = True,
    container_root: str = "/data",
) -> tuple[list[str], dict[str, str]]:
    """Return container paths and a container-to-host result mapping.

    ``exclude`` is a list of glob patterns matched against each entry's name *and* its
    path relative to the search root (POSIX form); a matching directory is pruned --
    skipped from the results and not descended into. When ``use_default_ignores`` is true
    (the default), the common VCS/dependency/cache names in :data:`DEFAULT_IGNORES` are
    excluded too. ``max_depth`` bounds how deep below the root to descend -- a direct child
    is depth 1 -- and ``None`` (the default) means unlimited. ``container_root`` is the
    in-container mount point the relative entries hang off (``/data`` for a single root;
    :func:`enumerate_roots` passes ``/data/0``, ``/data/1``, … to keep multiple roots from
    colliding).
    """
    root = search_root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Search root is not a directory: {root}")
    if max_depth is not None and max_depth < 1:
        raise ValueError("max_depth must be at least 1")

    patterns = [*exclude]
    if use_default_ignores:
        patterns += sorted(DEFAULT_IGNORES)

    container_paths: list[str] = []
    host_by_container: dict[str, str] = {}
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        depth = len(current_path.relative_to(root).parts)

        # Prune excluded directories in place so os.walk neither lists nor descends them.
        kept: list[str] = []
        for name in directories:
            relative_posix = (current_path / name).relative_to(root).as_posix()
            if not _matches_any(name, relative_posix, patterns):
                kept.append(name)
        directories[:] = kept

        for name in [*directories, *files]:
            host_path = current_path / name
            relative = host_path.relative_to(root)
            if name in files and _matches_any(name, relative.as_posix(), patterns):
                continue
            container_path = str(PurePosixPath(container_root, *relative.parts))
            container_paths.append(container_path)
            host_by_container[container_path] = str(host_path)

        # Stop descending once the next level would exceed max_depth; entries at the
        # current level (including directories) have already been recorded above.
        if max_depth is not None and depth + 1 >= max_depth:
            directories[:] = []
    return container_paths, host_by_container


def _normalize_roots(path: str | Path | Sequence[str | Path]) -> list[Path]:
    """Resolve one-or-many search paths to a de-duplicated list of existing roots.

    A bare string/``Path`` is treated as a single root; a sequence yields one root per
    entry. Each is expanded and resolved (``strict=True``, so a missing path raises), and
    exact duplicates are dropped so the same tree is never enumerated -- or mounted --
    twice. An empty sequence defaults to the current directory.
    """
    items: Sequence[str | Path]
    items = [path] if isinstance(path, (str, Path)) else list(path)
    if not items:
        items = ["."]
    roots: list[Path] = []
    seen: set[Path] = set()
    for item in items:
        root = Path(item).expanduser().resolve(strict=True)
        if root not in seen:
            seen.add(root)
            roots.append(root)
    return roots


def enumerate_roots(
    roots: Sequence[Path],
    *,
    exclude: Sequence[str] = (),
    max_depth: int | None = None,
    use_default_ignores: bool = True,
) -> tuple[list[str], dict[str, str], list[Mount]]:
    """Enumerate one or more search roots and return the mounts that expose them.

    For a single root this mounts it at ``/data`` (unchanged from the single-root past).
    For several roots, root *i* is mounted at ``/data/<i>`` and its entries are namespaced
    under that prefix, so identically named files in different roots never collide. The
    returned ``host_by_container`` map covers every root, and the ``Mount`` list is handed
    straight to :func:`run_filter`.
    """
    if not roots:
        raise ValueError("at least one search root is required")
    if len(roots) == 1:
        container_paths, host_by_container = enumerate_paths(
            roots[0], exclude=exclude, max_depth=max_depth, use_default_ignores=use_default_ignores
        )
        return container_paths, host_by_container, [Mount(roots[0], "/data", read_only=True)]

    container_paths = []
    host_by_container = {}
    mounts: list[Mount] = []
    for index, root in enumerate(roots):
        target = f"/data/{index}"
        paths, mapping = enumerate_paths(
            root,
            exclude=exclude,
            max_depth=max_depth,
            use_default_ignores=use_default_ignores,
            container_root=target,
        )
        container_paths.extend(paths)
        host_by_container.update(mapping)
        mounts.append(Mount(root, target, read_only=True))
    return container_paths, host_by_container, mounts


def build_worker_image(
    image: str = DEFAULT_IMAGE,
    dependencies: Sequence[str] = (),
    *,
    runtime: Runtime = PYTHON_RUNTIME,
    rebuild: bool = False,
    build_timeout: float = DEFAULT_BUILD_TIMEOUT,
    sandbox: Sandbox | None = None,
) -> str:
    """Ensure a runnable worker image and return the tag to run.

    With no dependencies this is the stdlib/runtime-only base image. With
    dependencies it builds (once, then caches) a derived image that layers the
    runtime's package install (``pip``/``npm``) on top of the base, and returns
    that derived tag. The actual Docker work is delegated to ``sandbox`` (a
    :class:`~nfind.sandbox.DockerSandbox` for the runtime by default); this function
    keeps only the nfind-specific ``Runtime``/dependency logic.
    """
    if sandbox is None:
        sandbox = DockerSandbox(
            image, dockerfile=_dockerfile_path(runtime.dockerfile), build_timeout=build_timeout
        )
    sandbox.ensure_image(rebuild=rebuild)
    if not dependencies:
        return image

    packages = sorted(set(dependencies))
    dockerfile_text = runtime.derived_dockerfile(image, packages)
    try:
        return sandbox.derive_image(dockerfile_text, rebuild=rebuild)
    except SandboxError as exc:
        # The sandbox is package-agnostic; restore the actionable list of packages.
        raise DockerError(
            f"Failed to build the worker image with packages ({', '.join(packages)}): {exc}"
        ) from exc


def _parse_worker_response(stdout: bytes) -> dict[str, Any]:
    """Decode and validate the worker's JSON protocol reply (nfind-specific)."""
    try:
        response = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Docker worker returned an invalid response.") from exc
    if not isinstance(response, dict) or response.get("ok") is not True:
        message = (
            response.get("error", "unknown worker error")
            if isinstance(response, dict)
            else "invalid response"
        )
        raise RuntimeError(f"Generated filter failed: {message}")
    return response


def run_filter(
    code: str,
    search_root: Path,
    container_paths: list[str],
    *,
    sandbox: Sandbox | None = None,
    image: str = DEFAULT_IMAGE,
    timeout: float = 10.0,
    memory: str = "256m",
    cpus: float = 1.0,
    pids_limit: int = 64,
    meta: dict[str, Any] | None = None,
    limits: Limits | None = None,
    mounts: list[Mount] | None = None,
) -> list[dict[str, Any]]:
    """Execute generated code in the sandbox and return container-path records.

    Builds the ``{code, paths, meta}`` request, hands it to ``sandbox.run`` (a
    :class:`~nfind.sandbox.DockerSandbox` for ``image`` by default), then validates the
    worker's ``{ok, results}`` reply against the supplied paths. The sandbox owns the
    hardened container and its limits; this adapter owns the worker protocol.

    Pass a :class:`~nfind.sandbox.Limits` as ``limits`` to set the resource/output caps
    directly; otherwise they are built from the ``timeout``/``memory``/``cpus``/
    ``pids_limit`` arguments (with the host's :data:`MAX_RESULT_BYTES` output cap).

    ``mounts`` overrides what is bound into the container; when omitted, ``search_root``
    is mounted read-only at ``/data`` (the single-root default). :func:`enumerate_roots`
    supplies multi-root mounts (``/data/0``, ``/data/1``, …).
    """
    if limits is None:
        limits = Limits(
            memory=memory,
            cpus=cpus,
            pids=pids_limit,
            timeout=timeout,
            max_output_bytes=MAX_RESULT_BYTES,
        )
    if limits.timeout <= 0 or limits.cpus <= 0 or limits.pids <= 0:
        raise ValueError("timeout, cpus, and pids must be positive")

    root = search_root.expanduser().resolve(strict=True)
    if mounts is None:
        mounts = [Mount(root, "/data", read_only=True)]
    if sandbox is None:
        sandbox = DockerSandbox(image, dockerfile=_dockerfile_path())
    request = json.dumps({"code": code, "paths": container_paths, "meta": meta or {}}).encode()

    try:
        run = sandbox.run(request, mounts=mounts, limits=limits)
    except SandboxTimeout as exc:
        raise TimeoutError(f"Generated filter exceeded the {limits.timeout:g}s timeout.") from exc
    except SandboxOutputTooLarge as exc:
        raise RuntimeError("Worker output exceeded the allowed size.") from exc

    if run.returncode != 0:
        error = run.stderr.decode(errors="replace").strip()
        detail = error or f"exit status {run.returncode}"
        raise RuntimeError(f"Docker worker failed: {detail}")

    response = _parse_worker_response(run.stdout)
    try:
        return _normalize_results(response.get("results"), set(container_paths))
    except ValueError as exc:
        raise RuntimeError(f"Generated filter returned an invalid result: {exc}") from exc


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

    ``path`` is a single directory or a sequence of directories; with several roots each
    is mounted separately and its entries are namespaced so identically named files don't
    collide. ``exclude`` (glob patterns), ``use_default_ignores`` (skip common
    VCS/dependency/cache directories), and ``max_depth`` (limit traversal depth) shape
    which paths are enumerated and handed to the filter; see :func:`enumerate_roots`.
    """
    roots = _normalize_roots(path)
    container_paths, host_by_container, mounts = enumerate_roots(
        roots, exclude=exclude, max_depth=max_depth, use_default_ignores=use_default_ignores
    )
    if not container_paths:
        return []
    # Verify Docker up front so a missing daemon fails before any API call.
    check_docker_available()
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
    )


def _run_generated(
    generated: GeneratedFilter,
    mounts: list[Mount],
    container_paths: list[str],
    host_by_container: dict[str, str],
    *,
    meta: dict[str, dict[str, Any]],
    image: str | None,
    timeout: float,
    memory: str,
    cpus: float,
    pids_limit: int,
    rebuild: bool,
    build_timeout: float,
    approve_dependencies: Callable[[list[str]], bool] | None,
    whitelist: set[str] | None,
    sandbox: Sandbox | None = None,
) -> list[dict[str, Any]]:
    """Build the sandbox image for a filter and run it, returning host-path records.

    Shared by ``search`` (freshly generated filters) and ``run_saved`` (filters
    replayed from a saved file). Unapproved third-party packages are gated through
    ``approve_dependencies``/the whitelist exactly as for a fresh generation.
    """
    runtime = RUNTIMES[generated.runtime]
    approved = whitelist if whitelist is not None else load_whitelist(runtime.name)
    new_packages = [pkg for pkg in generated.dependencies if pkg not in approved]
    if new_packages:
        if approve_dependencies is None or not approve_dependencies(new_packages):
            raise DependencyError(
                "filter requires packages that were not approved: " + ", ".join(new_packages)
            )
        approve_packages(new_packages, runtime.name)

    run_image = build_worker_image(
        image if image is not None else runtime.base_image,
        generated.dependencies,
        runtime=runtime,
        rebuild=rebuild,
        build_timeout=build_timeout,
        sandbox=sandbox,
    )
    records = run_filter(
        generated.code,
        mounts[0].source,
        container_paths,
        sandbox=sandbox,
        image=run_image,
        timeout=timeout,
        memory=memory,
        cpus=cpus,
        pids_limit=pids_limit,
        meta=meta,
        mounts=mounts,
    )
    host_records: list[dict[str, Any]] = []
    for record in records:
        mapped = dict(record)
        mapped["path"] = host_by_container[record["path"]]
        host_records.append(mapped)
    return host_records


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
    exclude: Sequence[str] = (),
    max_depth: int | None = None,
    use_default_ignores: bool = True,
) -> list[dict[str, Any]]:
    """Replay a previously saved filter through the sandbox, skipping the LLM.

    The file written by ``--save``/:func:`serialize_filter` is parsed back into a
    filter and run in the same hardened container as :func:`search`. Any third-party
    packages it declares are still gated through ``approve_dependencies``/the whitelist,
    so a saved filter cannot silently pull new packages. macOS metadata is not exposed
    on the replay path. ``path`` may be one directory or several (mounted and namespaced
    as in :func:`search`); ``exclude``/``use_default_ignores``/``max_depth`` shape
    enumeration exactly as for :func:`search`.
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
    check_docker_available()
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
    )
