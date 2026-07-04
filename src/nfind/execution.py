"""Run generated filters through the sandbox.

This module owns nfind-specific execution above the generic sandbox layer: building
runtime images, speaking the worker protocol, gating dependencies through the whitelist,
and mapping container-path records back to host paths.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .constants import (
    DEFAULT_BUILD_TIMEOUT,
    DEFAULT_CPUS,
    DEFAULT_IMAGE,
    DEFAULT_MEMORY,
    DEFAULT_PIDS_LIMIT,
    DEFAULT_TIMEOUT,
)
from .errors import DependencyError, DockerError
from .runtimes import PYTHON_RUNTIME, RUNTIMES, GeneratedFilter, Runtime
from .sandbox import (
    DEFAULT_SANDBOX_BACKEND,
    Limits,
    Mount,
    Sandbox,
    SandboxBackend,
    SandboxError,
    SandboxOutputTooLarge,
    SandboxTimeout,
    create_sandbox,
    dockerfile_path,
)
from .whitelist import approve_packages, load_whitelist
from .worker import MAX_RESULT_BYTES, _normalize_results


def build_worker_image(
    image: str = DEFAULT_IMAGE,
    dependencies: Sequence[str] = (),
    *,
    runtime: Runtime = PYTHON_RUNTIME,
    rebuild: bool = False,
    build_timeout: float = DEFAULT_BUILD_TIMEOUT,
    sandbox: Sandbox | None = None,
    sandbox_backend: SandboxBackend = DEFAULT_SANDBOX_BACKEND,
) -> str:
    """Ensure a runnable worker image and return the tag to run.

    With no dependencies this is the stdlib/runtime-only base image. With dependencies it
    builds (once, then caches) a derived image that layers the runtime's package install
    (``pip``/``npm``) on top of the base, and returns that derived tag. The actual Docker
    work is delegated to ``sandbox``; this function keeps only the nfind-specific
    ``Runtime``/dependency logic.
    """
    if sandbox is None:
        sandbox = create_sandbox(
            sandbox_backend,
            image,
            dockerfile=dockerfile_path(runtime.dockerfile),
            build_timeout=build_timeout,
        )
    sandbox.ensure_image(rebuild=rebuild)
    if not dependencies:
        return image

    packages = sorted(set(dependencies))
    dockerfile_text = runtime.derived_dockerfile(image, packages)
    try:
        return sandbox.derive_image(dockerfile_text, rebuild=rebuild)
    except SandboxError as exc:
        raise DockerError(
            f"Failed to build the worker image with packages ({', '.join(packages)}): {exc}"
        ) from exc


def _parse_worker_response(stdout: bytes) -> dict[str, Any]:
    """Decode and validate the worker's JSON protocol reply."""
    try:
        response = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Sandbox worker returned an invalid response.") from exc
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
    sandbox_backend: SandboxBackend = DEFAULT_SANDBOX_BACKEND,
    image: str = DEFAULT_IMAGE,
    timeout: float = DEFAULT_TIMEOUT,
    memory: str = DEFAULT_MEMORY,
    cpus: float = DEFAULT_CPUS,
    pids_limit: int = DEFAULT_PIDS_LIMIT,
    meta: dict[str, Any] | None = None,
    limits: Limits | None = None,
    mounts: list[Mount] | None = None,
) -> list[dict[str, Any]]:
    """Execute generated code in the sandbox and return container-path records."""
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
        sandbox = create_sandbox(sandbox_backend, image, dockerfile=dockerfile_path())
    request = json.dumps({"code": code, "paths": container_paths, "meta": meta or {}}).encode()

    try:
        run = sandbox.run(request, mounts=mounts, limits=limits)
    except SandboxTimeout as exc:
        raise TimeoutError(f"Generated filter exceeded the {limits.timeout:g}s timeout.") from exc
    except SandboxOutputTooLarge as exc:
        raise RuntimeError(
            f"Worker protocol response exceeded the {MAX_RESULT_BYTES:,}-byte safety limit."
        ) from exc

    if run.returncode != 0:
        error = run.stderr.decode(errors="replace").strip()
        detail = error or f"exit status {run.returncode}"
        raise RuntimeError(f"Sandbox worker failed: {detail}")

    response = _parse_worker_response(run.stdout)
    try:
        return _normalize_results(response.get("results"), set(container_paths))
    except ValueError as exc:
        raise RuntimeError(f"Generated filter returned an invalid result: {exc}") from exc


def run_generated(
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
    sandbox_backend: SandboxBackend = DEFAULT_SANDBOX_BACKEND,
) -> list[dict[str, Any]]:
    """Build the sandbox image for a filter and run it, returning host-path records."""
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
        sandbox_backend=sandbox_backend,
    )
    records = run_filter(
        generated.code,
        mounts[0].source,
        container_paths,
        sandbox=sandbox,
        sandbox_backend=sandbox_backend,
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
