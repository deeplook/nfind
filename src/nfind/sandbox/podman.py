"""Podman backend: availability probe, image build/derive, and the hardened run command.

Podman's CLI is drop-in compatible with the ``docker`` hardening flags, so
:class:`PodmanSandbox` reuses the shared Docker-family run command
(:func:`nfind.sandbox.docker.docker_family_run_command`) rather than keeping its own copy
-- the two backends therefore cannot drift on the security-critical flag set. What differs
is Podman-specific: it is typically daemonless (rootless), ``podman build`` has no
``--load`` flag, and its error messaging references ``podman machine`` rather than a
daemon. Rootless Podman also remaps the host user to root inside the container, so the
read-only ``/data`` mount would be unreadable by the non-root worker; this backend adds a
``--userns=keep-id`` mapping onto the worker's uid/gid to keep the mount readable without
weakening the shared hardening set (see :meth:`PodmanSandbox._userns_flags`).

The unit tests exercise this backend against mocked commands; the ``--userns`` remap was
additionally confirmed against a real rootless ``podman`` machine, but nfind still treats
this backend as experimental.
"""

from __future__ import annotations

import contextlib
import functools
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
from .docker import docker_family_run_command


def check_podman_available() -> None:
    """Fail early with an actionable error when Podman is unavailable."""
    try:
        completed = base._run_cli(
            ["podman", "ps", "--quiet", "--no-trunc"],
            capture_output=True,
            text=True,
            timeout=DOCKER_CHECK_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise SandboxUnavailable(
            "Podman CLI was not found. Install Podman and ensure 'podman' is on PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SandboxUnavailable(
            "Podman did not respond within 10 seconds. If you use 'podman machine', ensure "
            "it is running ('podman machine start'), then retry."
        ) from exc

    daemon_error = completed.stderr.strip()
    if completed.returncode != 0 or daemon_error:
        detail = _docker_error_detail(completed)
        raise SandboxUnavailable(
            f"Podman is unavailable: {detail}. Ensure Podman is set up "
            "(on macOS, run 'podman machine start'), then retry."
        )


def build_podman_image(
    image: str = DEFAULT_IMAGE,
    *,
    rebuild: bool = False,
    build_timeout: float = DEFAULT_BUILD_TIMEOUT,
    dockerfile: str = "Dockerfile.python",
) -> None:
    """Build the base worker image with Podman when absent, or unconditionally when asked."""
    if build_timeout <= 0:
        raise ValueError("build_timeout must be positive")
    check_podman_available()
    if not rebuild:
        try:
            probe = base._run_cli(
                ["podman", "image", "inspect", image],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=DOCKER_CHECK_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxUnavailable(
                "Podman timed out while inspecting the worker image. Restart Podman "
                "('podman machine stop' then 'podman machine start' on macOS), then retry."
            ) from exc
        if probe.returncode == 0:
            return

    dockerfile_path_value = dockerfile_path(dockerfile)
    try:
        completed = base._run_cli(
            [
                "podman",
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
            f"Podman worker image build exceeded the {build_timeout:g}s timeout. "
            "Restart Podman and retry."
        ) from exc
    if completed.returncode != 0:
        raise SandboxError(
            f"Podman worker image build failed with exit status {completed.returncode}. "
            "Verify Podman is set up ('podman info') and retry."
        )


def _podman_image_exists(image: str) -> bool:
    try:
        probe = base._run_cli(
            ["podman", "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=DOCKER_CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxUnavailable(
            "Podman timed out while inspecting the worker image. Restart Podman, then retry."
        ) from exc
    return probe.returncode == 0


@functools.lru_cache(maxsize=1)
def podman_is_rootless() -> bool:
    """Return whether Podman runs rootless on this host (cached for the process).

    Rootless Podman remaps the invoking host user to *root* inside the container's user
    namespace, so a read-only bind mount of a host-owned directory appears owned by root
    and is unreadable by the image's non-root worker user -- the run then finds nothing.
    Rootful Podman has no such remap (and rejects ``--userns=keep-id``), so the mapping
    must be applied only when this returns True. On any probe failure we assume rootful,
    which is the safe default: it never adds a flag that a rootful daemon would reject.
    """
    try:
        completed = base._run_cli(
            ["podman", "info", "--format", "{{.Host.Security.Rootless}}"],
            capture_output=True,
            text=True,
            timeout=DOCKER_CHECK_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and completed.stdout.strip().lower() == "true"


def _remove_podman_container(name: str) -> None:
    with contextlib.suppress(FileNotFoundError, subprocess.TimeoutExpired):
        base._run_cli(
            ["podman", "rm", "--force", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )


class PodmanSandbox(_CliSandbox):
    """Concrete :class:`~nfind.sandbox.base.Sandbox` backed by the ``podman`` CLI.

    Reuses the shared Docker-family hardened run command and adds only Podman-specific
    build/probe/remove calls and messaging. It does not interpret exit codes or parse
    output -- that is the caller's job.
    """

    _derived_noun = "derived Podman image"
    _restart_hint = "Restart Podman and retry."

    def check_available(self) -> None:
        """Verify Podman is reachable."""
        check_podman_available()

    def ensure_image(self, *, rebuild: bool = False) -> None:
        """Build the base image when absent, or unconditionally when requested."""
        build_podman_image(
            self.image,
            rebuild=rebuild,
            build_timeout=self.build_timeout,
            dockerfile=self._dockerfile.name,
        )

    def _image_present(self, image: str) -> bool:
        return _podman_image_exists(image)

    def _build_derived_command(self, derived: str, context: str) -> list[str]:
        return ["podman", "build", "--tag", derived, context]

    def _remove(self, name: str) -> None:
        _remove_podman_container(name)

    def _userns_flags(self) -> list[str]:
        """Return the rootless user-namespace remap so the worker can read bind mounts.

        Rootless Podman maps the host user to root in the container; without a remap the
        read-only ``/data`` mount is owned by root and unreadable by the non-root worker,
        which silently yields no results. ``keep-id`` mapped onto the worker's own uid/gid
        makes the mount appear owned by the worker inside the container. This is skipped in
        rootful mode (where ``keep-id`` is invalid) and when the worker uid is unknown.
        """
        if self.run_uid is None or self.run_gid is None or not podman_is_rootless():
            return []
        return ["--userns", f"keep-id:uid={self.run_uid},gid={self.run_gid}"]

    def _build_run_command(self, name: str, mounts: Sequence[Mount], limits: Limits) -> list[str]:
        """Assemble the hardened ``podman run`` invocation (shared Docker-family flags)."""
        return docker_family_run_command(
            "podman", self.image, name, mounts, limits, extra_flags=self._userns_flags()
        )
