"""A small, domain-agnostic sandbox for running untrusted code in Docker.

This module knows nothing about prompts, paths, filters, or the worker protocol; it
owns only the *generic* half of pfind's execution: building/deriving a Docker image and
running a single hardened, disposable container with stdin piped in and stdout/stderr
captured. The security-relevant ``docker run`` flag set lives in exactly one place
(:meth:`DockerSandbox._docker_run_command`) so it can be audited at a glance.

The :class:`Sandbox` ``Protocol`` lets callers swap in a fake for tests (no Docker
needed) or an alternate backend (Podman, gVisor, rootless) later without touching the
domain logic that drives it.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import signal
import subprocess
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .constants import DEFAULT_BUILD_TIMEOUT, DEFAULT_IMAGE, DOCKER_CHECK_TIMEOUT


@dataclass(frozen=True)
class Mount:
    """A host directory bound into the container at ``target``."""

    source: Path
    target: str
    read_only: bool = True


@dataclass(frozen=True)
class Limits:
    """Resource and output limits for a single sandboxed run."""

    memory: str = "256m"
    cpus: float = 1.0
    pids: int = 64
    timeout: float = 10.0
    max_output_bytes: int = 1_000_000


@dataclass(frozen=True)
class CompletedRun:
    """The raw outcome of a sandboxed run; the caller interprets it."""

    stdout: bytes
    stderr: bytes
    returncode: int


class SandboxError(RuntimeError):
    """Base class for actionable sandbox lifecycle failures (build/run)."""


class SandboxUnavailable(SandboxError):
    """Raised when the backend CLI or daemon cannot be reached."""


class SandboxTimeout(SandboxError):
    """Raised when a run exceeds ``Limits.timeout``."""


class SandboxOutputTooLarge(SandboxError):
    """Raised when stdout/stderr exceed ``Limits.max_output_bytes``."""


class Sandbox(Protocol):
    """The capability pfind needs from an execution backend."""

    def ensure_image(self, *, rebuild: bool = False) -> None: ...

    def derive_image(self, dockerfile_text: str, *, rebuild: bool = False) -> str: ...

    def run(self, stdin: bytes, *, mounts: list[Mount], limits: Limits) -> CompletedRun: ...


def _dockerfile_path(name: str = "Dockerfile.python") -> Path:
    """Locate a Dockerfile packaged next to this module."""
    return Path(__file__).with_name(name)


def _docker_error_detail(completed: subprocess.CompletedProcess[str]) -> str:
    detail = (completed.stderr or completed.stdout or "unknown error").strip()
    return detail[-500:]


def _run_docker(
    command: list[str],
    *,
    timeout: float,
    input: bytes | str | None = None,
    capture_output: bool = False,
    text: bool = False,
    stdout: int | None = None,
    stderr: int | None = None,
) -> subprocess.CompletedProcess[Any]:
    """Run Docker and kill its whole CLI/plugin process group on timeout."""
    captured_stdout = None
    captured_stderr = None
    if capture_output:
        if stdout is not None or stderr is not None:
            raise ValueError("stdout and stderr cannot be used with capture_output")
        # Files, rather than pipes, are deliberate. Docker Desktop plugins can
        # daemonize and retain inherited pipes after the CLI exits or is killed,
        # causing subprocess.communicate() to wait forever for EOF.
        captured_stdout = tempfile.TemporaryFile()  # noqa: SIM115 - closed below
        captured_stderr = tempfile.TemporaryFile()  # noqa: SIM115 - closed below
        stdout = captured_stdout.fileno()
        stderr = captured_stderr.fileno()

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if input is not None else None,
        stdout=stdout,
        stderr=stderr,
        text=text,
        start_new_session=os.name == "posix",
    )
    try:
        output, errors = process.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)
        if process.poll() is None:
            process.kill()
            process.wait()
        output, errors = None, None
        if captured_stdout is not None and captured_stderr is not None:
            captured_stdout.seek(0)
            captured_stderr.seek(0)
            output = captured_stdout.read()
            errors = captured_stderr.read()
            if text:
                output = output.decode(errors="replace")
                errors = errors.decode(errors="replace")
            captured_stdout.close()
            captured_stderr.close()
        raise subprocess.TimeoutExpired(
            command, timeout, output=output or exc.output, stderr=errors or exc.stderr
        ) from exc

    if captured_stdout is not None and captured_stderr is not None:
        captured_stdout.seek(0)
        captured_stderr.seek(0)
        output = captured_stdout.read()
        errors = captured_stderr.read()
        if text:
            output = output.decode(errors="replace")
            errors = errors.decode(errors="replace")
        captured_stdout.close()
        captured_stderr.close()
    return subprocess.CompletedProcess(command, process.returncode, output, errors)


def check_docker_available() -> None:
    """Fail early with an actionable error when the Docker daemon is unavailable."""
    try:
        completed = _run_docker(
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
            probe = _run_docker(
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

    dockerfile_path = _dockerfile_path(dockerfile)
    try:
        completed = _run_docker(
            [
                "docker",
                "build",
                "--load",
                "--file",
                str(dockerfile_path),
                "--tag",
                image,
                str(dockerfile_path.parent),
            ],
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
        probe = _run_docker(
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


def _derived_image_tag(image: str, dockerfile_text: str) -> str:
    """Stable, content-hashed tag for an image derived from ``dockerfile_text``."""
    repository = image.split(":", 1)[0]
    digest = hashlib.sha256(dockerfile_text.encode()).hexdigest()[:12]
    return f"{repository}:deps-{digest}"


def _remove_container(name: str) -> None:
    with contextlib.suppress(FileNotFoundError, subprocess.TimeoutExpired):
        _run_docker(
            ["docker", "rm", "--force", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )


class DockerSandbox:
    """Concrete :class:`Sandbox` backed by the ``docker`` CLI.

    Owns the hardened ``docker run`` flag set and the image build/derive mechanics.
    It does not interpret exit codes or parse output -- that is the caller's job.
    """

    def __init__(
        self,
        image: str,
        *,
        dockerfile: str | Path = "Dockerfile.python",
        build_timeout: float = DEFAULT_BUILD_TIMEOUT,
        name_prefix: str = "pfind-search-",
    ) -> None:
        self.image = image
        self._dockerfile = Path(dockerfile)
        self.build_timeout = build_timeout
        self.name_prefix = name_prefix

    def ensure_image(self, *, rebuild: bool = False) -> None:
        """Build the base image when absent, or unconditionally when requested."""
        build_image(
            self.image,
            rebuild=rebuild,
            build_timeout=self.build_timeout,
            dockerfile=self._dockerfile.name,
        )

    def derive_image(self, dockerfile_text: str, *, rebuild: bool = False) -> str:
        """Build (once, then cache) an image from ``dockerfile_text``; return its tag.

        The tag is content-hashed from the Dockerfile text, so identical derived images
        are reused across runs.
        """
        derived = _derived_image_tag(self.image, dockerfile_text)
        if not rebuild and _image_exists(derived):
            return derived
        with tempfile.TemporaryDirectory(prefix="pfind-deps-") as context:
            (Path(context) / "Dockerfile").write_text(dockerfile_text)
            try:
                completed = _run_docker(
                    ["docker", "build", "--load", "--tag", derived, context],
                    timeout=self.build_timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise SandboxError(
                    f"Building the derived sandbox image exceeded the "
                    f"{self.build_timeout:g}s timeout. Restart Docker and retry."
                ) from exc
        if completed.returncode != 0:
            raise SandboxError(
                f"Failed to build the derived sandbox image; exit status {completed.returncode}."
            )
        return derived

    def _docker_run_command(self, name: str, mounts: Sequence[Mount], limits: Limits) -> list[str]:
        """Assemble the single, auditable hardened ``docker run`` invocation.

        Every security-relevant flag lives here: no network, read-only root, all
        capabilities dropped, no privilege escalation, and process/memory/CPU/file-
        descriptor/tmpfs limits.
        """
        command = [
            "docker",
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
        command.append(self.image)
        return command

    def run(self, stdin: bytes, *, mounts: list[Mount], limits: Limits) -> CompletedRun:
        """Run the image in a disposable, hardened container with ``stdin`` piped in."""
        name = f"{self.name_prefix}{uuid.uuid4().hex}"
        command = self._docker_run_command(name, mounts, limits)
        try:
            completed = _run_docker(
                command,
                input=stdin,
                capture_output=True,
                timeout=limits.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            _remove_container(name)
            raise SandboxTimeout(f"Sandbox run exceeded the {limits.timeout:g}s timeout.") from exc

        if (
            len(completed.stdout) > limits.max_output_bytes
            or len(completed.stderr) > limits.max_output_bytes
        ):
            raise SandboxOutputTooLarge("Worker output exceeded the allowed size.")
        return CompletedRun(
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )
