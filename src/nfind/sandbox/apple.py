"""Apple Containers backend: availability probe, image build/derive, and run command.

Apple Containers use OCI images and Dockerfile-compatible builds, but their run flags are
not a byte-for-byte Docker match. In particular, macOS 15 does not accept
``--network none``; macOS 26+ does, so nfind selects the strongest network flag the host
supports. The CLI does not expose Docker's ``--pids-limit`` or
``--security-opt no-new-privileges`` options. The supported hardening flags stay in one
auditable method (:meth:`AppleContainerSandbox._build_run_command`) so the differences
from Docker remain visible. Everything generic comes from :mod:`nfind.sandbox.base`.
"""

from __future__ import annotations

import contextlib
import platform
import subprocess
from collections.abc import Sequence

from ..constants import DEFAULT_BUILD_TIMEOUT, DEFAULT_IMAGE, DOCKER_CHECK_TIMEOUT
from . import base
from .base import (
    Limits,
    Mount,
    SandboxError,
    SandboxUnavailable,
    _CliSandbox,
    _docker_error_detail,
    dockerfile_path,
)


def check_apple_container_available() -> None:
    """Fail early with an actionable error when Apple container services are unavailable."""
    try:
        completed = base._run_cli(
            ["container", "system", "status", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=DOCKER_CHECK_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise SandboxUnavailable(
            "Apple container CLI was not found. Install Apple Containers and ensure "
            "'container' is on PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SandboxUnavailable(
            "Apple container services did not respond within 10 seconds. Run "
            "'container system start' or restart the services, then retry."
        ) from exc

    if completed.returncode != 0:
        detail = _docker_error_detail(completed)
        raise SandboxUnavailable(
            f"Apple container services are unavailable: {detail}. Run "
            "'container system start', then retry."
        )


def build_apple_container_image(
    image: str = DEFAULT_IMAGE,
    *,
    rebuild: bool = False,
    build_timeout: float = DEFAULT_BUILD_TIMEOUT,
    dockerfile: str = "Dockerfile.python",
) -> None:
    """Build the base worker image with Apple Containers when absent/requested."""
    if build_timeout <= 0:
        raise ValueError("build_timeout must be positive")
    check_apple_container_available()
    if not rebuild:
        try:
            probe = base._run_cli(
                ["container", "image", "inspect", image],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=DOCKER_CHECK_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxUnavailable(
                "Apple container timed out while inspecting the worker image. "
                "Restart Apple container services, then retry."
            ) from exc
        if probe.returncode == 0:
            return

    dockerfile_path_value = dockerfile_path(dockerfile)
    try:
        completed = base._run_cli(
            [
                "container",
                "build",
                "--file",
                str(dockerfile_path_value),
                "--tag",
                image,
                str(dockerfile_path_value.parent),
            ],
            timeout=build_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxError(
            f"Apple container worker image build exceeded the {build_timeout:g}s timeout. "
            "Restart Apple container services and retry."
        ) from exc
    if completed.returncode != 0:
        raise SandboxError(
            f"Apple container worker image build failed with exit status {completed.returncode}. "
            "Verify it with 'container system status' and retry."
        )


def _apple_image_exists(image: str) -> bool:
    try:
        probe = base._run_cli(
            ["container", "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=DOCKER_CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxUnavailable(
            "Apple container timed out while inspecting the worker image. "
            "Restart Apple container services, then retry."
        ) from exc
    return probe.returncode == 0


def _remove_apple_container(name: str) -> None:
    with contextlib.suppress(FileNotFoundError, subprocess.TimeoutExpired):
        base._run_cli(
            ["container", "delete", "--force", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )


def _apple_cpus_arg(cpus: float) -> str:
    """Return the Apple Containers ``--cpus`` value.

    Docker accepts fractional CPU values such as ``1.0``. Apple Containers 1.0 rejects
    that spelling and expects a whole-number CPU count, so preserve nfind's default
    ``1.0`` by formatting integral floats as integers and fail early with a clear error
    for fractional values.
    """
    if not cpus.is_integer():
        raise ValueError(
            "Apple Containers requires --cpus to be a whole number; "
            f"got {cpus:g}. Use Docker for fractional CPU limits."
        )
    return str(int(cpus))


def _macos_major_version() -> int | None:
    """Return the host macOS major version, or ``None`` outside/unknown macOS."""
    version = platform.mac_ver()[0]
    if not version:
        return None
    try:
        return int(version.split(".", maxsplit=1)[0])
    except ValueError:
        return None


def apple_supports_no_network_flag() -> bool:
    """Return whether Apple Containers should support ``--network none`` on this host."""
    major = _macos_major_version()
    return major is not None and major >= 26


def _apple_network_args() -> list[str]:
    """Return Apple ``container run`` network-isolation flags for this macOS version."""
    if apple_supports_no_network_flag():
        return ["--network", "none"]
    return ["--no-dns"]


class AppleContainerSandbox(_CliSandbox):
    """Concrete :class:`~nfind.sandbox.base.Sandbox` backed by Apple's ``container`` CLI.

    Owns the supported Apple-Containers run flags and the Apple-specific
    build/probe/remove calls. See the module docstring for how its hardening differs from
    Docker's.
    """

    _derived_noun = "derived Apple container image"
    _restart_hint = "Restart Apple container services and retry."

    def check_available(self) -> None:
        """Verify Apple container services are reachable."""
        check_apple_container_available()

    def ensure_image(self, *, rebuild: bool = False) -> None:
        """Build the base image when absent, or unconditionally when requested."""
        build_apple_container_image(
            self.image,
            rebuild=rebuild,
            build_timeout=self.build_timeout,
            dockerfile=self._dockerfile.name,
        )

    def _image_present(self, image: str) -> bool:
        return _apple_image_exists(image)

    def _build_derived_command(self, derived: str, context: str) -> list[str]:
        return ["container", "build", "--tag", derived, context]

    def _remove(self, name: str) -> None:
        _remove_apple_container(name)

    def _build_run_command(self, name: str, mounts: Sequence[Mount], limits: Limits) -> list[str]:
        """Assemble the Apple ``container run`` invocation.

        The CLI supports read-only rootfs, capability dropping, memory/CPU limits,
        ulimits, tmpfs, and read-only bind mounts. On macOS 26+ it also accepts
        ``--network none``; older macOS releases fall back to ``--no-dns`` because
        ``--network`` is rejected there. It intentionally omits Docker-only flags that
        Apple Containers currently reject or do not document.
        """
        command = [
            "container",
            "run",
            "--rm",
            "--name",
            name,
            "--interactive",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--memory",
            limits.memory,
            "--cpus",
            _apple_cpus_arg(limits.cpus),
            "--ulimit",
            "nofile=128:128",
            *_apple_network_args(),
            "--tmpfs",
            "/tmp",
        ]
        for mount in mounts:
            readonly = ",readonly" if mount.read_only else ""
            command += [
                "--mount",
                f"type=bind,source={mount.source},target={mount.target}{readonly}",
            ]
        command.append(self.image)
        return command
