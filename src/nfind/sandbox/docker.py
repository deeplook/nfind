"""Docker backend: availability probe, image build/derive, and the hardened run command.

The security-relevant ``docker run`` flag set lives in exactly one place
(:meth:`DockerSandbox._build_run_command`) so it can be audited at a glance. Everything
generic -- the value types, the subprocess runner, and the ``run``/``derive_image``
mechanics -- comes from :mod:`nfind.sandbox.base`.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
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


def check_docker_available() -> None:
    """Fail early with an actionable error when the Docker daemon is unavailable."""
    try:
        completed = base._run_cli(
            ["docker", "ps", "--quiet", "--no-trunc"],
            capture_output=True,
            text=True,
            timeout=DOCKER_CHECK_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise SandboxUnavailable(
            "Docker CLI was not found. Install Docker and ensure 'docker' is on PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SandboxUnavailable(
            "Docker daemon did not respond within 10 seconds. Start or restart Docker, then retry."
        ) from exc

    daemon_error = completed.stderr.strip()
    if completed.returncode != 0 or daemon_error:
        detail = _docker_error_detail(completed)
        raise SandboxUnavailable(
            f"Docker daemon is unavailable: {detail}. "
            "Start Docker Desktop (macOS/Windows) or the Docker daemon (Linux), then retry."
        )


def _docker_build_supports_load() -> bool:
    """Return whether ``docker build`` accepts ``--load`` in this environment."""
    try:
        completed = base._run_cli(
            ["docker", "build", "--help"],
            capture_output=True,
            text=True,
            timeout=DOCKER_CHECK_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and "--load" in completed.stdout


def _docker_build_command(*, dockerfile: str | None, tag: str, context: str) -> list[str]:
    """Build a portable Docker image-build command for this host."""
    command = ["docker", "build"]
    if _docker_build_supports_load():
        command.append("--load")
    if dockerfile is not None:
        command.extend(["--file", dockerfile])
    command.extend(["--tag", tag, context])
    return command


def docker_supports_linux_containers() -> bool:
    """Return whether the reachable Docker daemon is configured for Linux containers."""
    try:
        completed = base._run_cli(
            ["docker", "info", "--format", "{{.OSType}}"],
            capture_output=True,
            text=True,
            timeout=DOCKER_CHECK_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and completed.stdout.strip().lower() == "linux"


def build_image(
    image: str = DEFAULT_IMAGE,
    *,
    rebuild: bool = False,
    build_timeout: float = DEFAULT_BUILD_TIMEOUT,
    dockerfile: str = "Dockerfile.python",
) -> None:
    """Build the base worker image when absent, or unconditionally when requested."""
    if build_timeout <= 0:
        raise ValueError("build_timeout must be positive")
    check_docker_available()
    if not rebuild:
        try:
            probe = base._run_cli(
                ["docker", "image", "inspect", image],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=DOCKER_CHECK_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxUnavailable(
                "Docker timed out while inspecting the worker image. Restart Docker, then retry."
            ) from exc
        if probe.returncode == 0:
            return

    # Frame the raw BuildKit output that follows (it streams to stderr) so a first-time
    # user knows the pause is an expected one-off image build, not a hang.
    print("nfind: building the sandbox worker image (first run only)...", file=sys.stderr)
    dockerfile_path_value = dockerfile_path(dockerfile)
    try:
        completed = base._run_cli(
            _docker_build_command(
                dockerfile=str(dockerfile_path_value),
                tag=image,
                context=str(dockerfile_path_value.parent),
            ),
            timeout=build_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxError(
            f"Docker worker image build exceeded the {build_timeout:g}s timeout. "
            "Restart Docker and retry."
        ) from exc
    if completed.returncode != 0:
        raise SandboxError(
            f"Docker worker image build failed with exit status {completed.returncode}. "
            "The daemon may have stopped; verify it with 'docker info' and retry."
        )


def _image_exists(image: str) -> bool:
    try:
        probe = base._run_cli(
            ["docker", "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=DOCKER_CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxUnavailable(
            "Docker timed out while inspecting the worker image. Restart Docker, then retry."
        ) from exc
    return probe.returncode == 0


def _remove_container(name: str) -> None:
    with contextlib.suppress(FileNotFoundError, subprocess.TimeoutExpired):
        base._run_cli(
            ["docker", "rm", "--force", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )


def docker_family_run_command(
    executable: str, image: str, name: str, mounts: Sequence[Mount], limits: Limits
) -> list[str]:
    """Assemble the hardened ``<executable> run`` invocation shared by the Docker family.

    ``docker`` and ``podman`` accept the identical hardening flag set, so both backends
    build their run command here rather than each keeping its own copy. This is the single
    security-relevant flag set to audit: no network, read-only root, all capabilities
    dropped, no privilege escalation, and process/memory/CPU/file-descriptor/tmpfs limits.
    Apple Containers differ and keep their own command (see :mod:`nfind.sandbox.apple`).
    """
    command = [
        executable,
        "run",
        "--rm",
        "--name",
        name,
        "--interactive",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(limits.pids),
        "--memory",
        limits.memory,
        "--cpus",
        str(limits.cpus),
        "--ulimit",
        "nofile=128:128",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=16m",
    ]
    for mount in mounts:
        readonly = ",readonly" if mount.read_only else ""
        command += [
            "--mount",
            f"type=bind,src={mount.source},dst={mount.target}{readonly}",
        ]
    command.append(image)
    return command


class DockerSandbox(_CliSandbox):
    """Concrete :class:`~nfind.sandbox.base.Sandbox` backed by the ``docker`` CLI.

    Owns the hardened ``docker run`` flag set and the Docker-specific build/probe/remove
    calls. It does not interpret exit codes or parse output -- that is the caller's job.
    """

    def check_available(self) -> None:
        """Verify Docker is reachable."""
        check_docker_available()

    def ensure_image(self, *, rebuild: bool = False) -> None:
        """Build the base image when absent, or unconditionally when requested."""
        build_image(
            self.image,
            rebuild=rebuild,
            build_timeout=self.build_timeout,
            dockerfile=self._dockerfile.name,
        )

    def _image_present(self, image: str) -> bool:
        return _image_exists(image)

    def _build_derived_command(self, derived: str, context: str) -> list[str]:
        return _docker_build_command(dockerfile=None, tag=derived, context=context)

    def _remove(self, name: str) -> None:
        _remove_container(name)

    def _build_run_command(self, name: str, mounts: Sequence[Mount], limits: Limits) -> list[str]:
        """Assemble the hardened ``docker run`` invocation (shared Docker-family flags)."""
        return docker_family_run_command("docker", self.image, name, mounts, limits)
