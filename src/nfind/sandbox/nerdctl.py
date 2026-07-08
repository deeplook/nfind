"""nerdctl (containerd) backend: availability probe, image build/derive, and run command.

nerdctl's CLI is deliberately Docker-compatible, so :class:`NerdctlSandbox` reuses the
shared Docker-family hardened run command
(:func:`nfind.sandbox.docker.docker_family_run_command`) rather than keeping its own copy
-- the backends therefore cannot drift on the security-critical flag set. What differs is
nerdctl-specific: it talks to ``containerd`` (typically via BuildKit for builds), so
``nerdctl build`` writes straight into the containerd image store and needs no Docker
``--load`` flag, and its error messaging references ``containerd``/``nerdctl`` rather than a
Docker daemon.

Unlike the Podman backend, nerdctl does **not** support ``--userns=keep-id:uid=...,gid=...``
(its ``--userns`` handling is limited), so the rootless mount-readability remap nfind
applies for rootless Podman cannot be reused here. On rootless nerdctl the read-only mount
may therefore appear owned by root and be unreadable by the image's non-root worker; this is
a known open caveat.

nerdctl support is validated only against mocked commands; the exact runtime behavior of the
real ``nerdctl`` CLI is unverified, so nfind treats this backend as experimental.
"""

from __future__ import annotations

import contextlib
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


def check_nerdctl_available() -> None:
    """Fail early with an actionable error when nerdctl/containerd is unavailable."""
    try:
        completed = base._run_cli(
            ["nerdctl", "ps", "--quiet", "--no-trunc"],
            capture_output=True,
            text=True,
            timeout=DOCKER_CHECK_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise SandboxUnavailable(
            "nerdctl CLI was not found. Install nerdctl and ensure 'nerdctl' is on PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SandboxUnavailable(
            "nerdctl did not respond within 10 seconds. Ensure containerd is running "
            "(and, on a Lima/Rancher Desktop VM, that the VM is started), then retry."
        ) from exc

    daemon_error = completed.stderr.strip()
    if completed.returncode != 0 or daemon_error:
        detail = _docker_error_detail(completed)
        raise SandboxUnavailable(
            f"nerdctl is unavailable: {detail}. Ensure containerd is running "
            "('nerdctl info'), then retry."
        )


def build_nerdctl_image(
    image: str = DEFAULT_IMAGE,
    *,
    rebuild: bool = False,
    build_timeout: float = DEFAULT_BUILD_TIMEOUT,
    dockerfile: str = "Dockerfile.python",
) -> None:
    """Build the base worker image with nerdctl when absent, or unconditionally when asked."""
    if build_timeout <= 0:
        raise ValueError("build_timeout must be positive")
    check_nerdctl_available()
    if not rebuild:
        try:
            probe = base._run_cli(
                ["nerdctl", "image", "inspect", image],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=DOCKER_CHECK_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxUnavailable(
                "nerdctl timed out while inspecting the worker image. Restart containerd, "
                "then retry."
            ) from exc
        if probe.returncode == 0:
            return

    dockerfile_path_value = dockerfile_path(dockerfile)
    try:
        completed = base._run_cli(
            [
                "nerdctl",
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
            f"nerdctl worker image build exceeded the {build_timeout:g}s timeout. "
            "Restart containerd and retry."
        ) from exc
    if completed.returncode != 0:
        raise SandboxError(
            f"nerdctl worker image build failed with exit status {completed.returncode}. "
            "Verify containerd is set up ('nerdctl info') and retry."
        )


def _nerdctl_image_exists(image: str) -> bool:
    try:
        probe = base._run_cli(
            ["nerdctl", "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=DOCKER_CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxUnavailable(
            "nerdctl timed out while inspecting the worker image. Restart containerd, then retry."
        ) from exc
    return probe.returncode == 0


def _remove_nerdctl_container(name: str) -> None:
    with contextlib.suppress(FileNotFoundError, subprocess.TimeoutExpired):
        base._run_cli(
            ["nerdctl", "rm", "--force", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )


class NerdctlSandbox(_CliSandbox):
    """Concrete :class:`~nfind.sandbox.base.Sandbox` backed by the ``nerdctl`` CLI.

    Reuses the shared Docker-family hardened run command and adds only nerdctl-specific
    build/probe/remove calls and messaging. It does not interpret exit codes or parse
    output -- that is the caller's job.
    """

    _derived_noun = "derived nerdctl image"
    _restart_hint = "Restart containerd and retry."

    def check_available(self) -> None:
        """Verify nerdctl/containerd is reachable."""
        check_nerdctl_available()

    def ensure_image(self, *, rebuild: bool = False) -> None:
        """Build the base image when absent, or unconditionally when requested."""
        build_nerdctl_image(
            self.image,
            rebuild=rebuild,
            build_timeout=self.build_timeout,
            dockerfile=self._dockerfile.name,
        )

    def _image_present(self, image: str) -> bool:
        return _nerdctl_image_exists(image)

    def _build_derived_command(self, derived: str, context: str) -> list[str]:
        return ["nerdctl", "build", "--tag", derived, context]

    def _remove(self, name: str) -> None:
        _remove_nerdctl_container(name)

    def _build_run_command(self, name: str, mounts: Sequence[Mount], limits: Limits) -> list[str]:
        """Assemble the hardened ``nerdctl run`` invocation (shared Docker-family flags)."""
        return docker_family_run_command("nerdctl", self.image, name, mounts, limits)
