"""A small, domain-agnostic sandbox for running untrusted code in containers.

This package knows nothing about prompts, paths, filters, or the worker protocol; it owns
only the *generic* half of nfind's execution: building/deriving a container image and
running a single hardened, disposable container with stdin piped in and stdout/stderr
captured.

The pieces are split by concern so each backend's security-relevant flag set can be
audited at a glance and a new backend (e.g. Podman) is a small addition:

* :mod:`~nfind.sandbox.base` -- value types, error hierarchy, the :class:`Sandbox`
  ``Protocol``, the shared subprocess runner, and :class:`~nfind.sandbox.base._CliSandbox`,
  the template base class implementing the identical ``run``/``derive_image`` mechanics.
* :mod:`~nfind.sandbox.docker` -- the Docker backend and the hardened, Docker-family run
  command shared with Podman.
* :mod:`~nfind.sandbox.podman` -- the Podman backend (experimental; reuses the
  Docker-family run command).
* :mod:`~nfind.sandbox.apple` -- the Apple Containers backend and its run command.

The :class:`Sandbox` ``Protocol`` lets callers swap in a fake for tests (no container
runtime needed) or an alternate backend without touching the domain logic that drives it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, get_args

from ..constants import DEFAULT_BUILD_TIMEOUT
from .apple import (
    AppleContainerSandbox,
    apple_supports_no_network_flag,
    build_apple_container_image,
    check_apple_container_available,
)
from .base import (
    CompletedRun,
    Limits,
    Mount,
    Sandbox,
    SandboxError,
    SandboxOutputTooLarge,
    SandboxTimeout,
    SandboxUnavailable,
    derived_image_tag,
    dockerfile_path,
)
from .docker import (
    DockerSandbox,
    build_image,
    check_docker_available,
    docker_supports_linux_containers,
)
from .podman import (
    PodmanSandbox,
    build_podman_image,
    check_podman_available,
)

SandboxBackend = Literal["docker", "apple", "podman"]
SANDBOX_BACKENDS = get_args(SandboxBackend)
DEFAULT_SANDBOX_BACKEND: SandboxBackend = "docker"


def check_sandbox_available(backend: SandboxBackend = DEFAULT_SANDBOX_BACKEND) -> None:
    """Check availability for the selected sandbox backend."""
    if backend == "docker":
        check_docker_available()
        return
    if backend == "apple":
        check_apple_container_available()
        return
    if backend == "podman":
        check_podman_available()
        return
    raise ValueError(f"Unsupported sandbox backend: {backend}")


def create_sandbox(
    backend: SandboxBackend,
    image: str,
    *,
    dockerfile: str | Path = "Dockerfile.python",
    build_timeout: float = DEFAULT_BUILD_TIMEOUT,
) -> Sandbox:
    """Create a concrete sandbox for ``backend``."""
    if backend == "docker":
        return DockerSandbox(image, dockerfile=dockerfile, build_timeout=build_timeout)
    if backend == "apple":
        return AppleContainerSandbox(image, dockerfile=dockerfile, build_timeout=build_timeout)
    if backend == "podman":
        return PodmanSandbox(image, dockerfile=dockerfile, build_timeout=build_timeout)
    raise ValueError(f"Unsupported sandbox backend: {backend}")


# ``nfind.sandbox`` is the package's public boundary: it re-exports the value types,
# errors, backend classes, and dispatch helpers that the rest of nfind imports through
# ``from .sandbox import ...``. ``__all__`` names exactly that surface so mypy's strict
# ``no_implicit_reexport`` and ruff's unused-import check pass without per-import aliases.
# Image path/tag helpers are re-exported because sibling modules and tests use them.
__all__ = [
    # Dispatch and backend selection.
    "SandboxBackend",
    "SANDBOX_BACKENDS",
    "DEFAULT_SANDBOX_BACKEND",
    "check_sandbox_available",
    "create_sandbox",
    # Value types and Protocol.
    "Mount",
    "Limits",
    "CompletedRun",
    "Sandbox",
    # Error hierarchy.
    "SandboxError",
    "SandboxUnavailable",
    "SandboxTimeout",
    "SandboxOutputTooLarge",
    # Docker backend.
    "DockerSandbox",
    "build_image",
    "check_docker_available",
    "docker_supports_linux_containers",
    # Apple Containers backend.
    "AppleContainerSandbox",
    "build_apple_container_image",
    "check_apple_container_available",
    "apple_supports_no_network_flag",
    # Podman backend.
    "PodmanSandbox",
    "build_podman_image",
    "check_podman_available",
    # Helpers used by sibling modules.
    "dockerfile_path",
    "derived_image_tag",
]
