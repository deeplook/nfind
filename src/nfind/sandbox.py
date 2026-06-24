"""A small, domain-agnostic sandbox for running untrusted code in Docker.

This module knows nothing about prompts, paths, filters, or the worker protocol; it
owns only the *generic* half of nfind's execution: building/deriving a Docker image and
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
import platform
import signal
import subprocess
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from .constants import DEFAULT_BUILD_TIMEOUT, DEFAULT_IMAGE, DOCKER_CHECK_TIMEOUT

SandboxBackend = Literal["docker", "apple"]
DEFAULT_SANDBOX_BACKEND: SandboxBackend = "docker"


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
    """The capability nfind needs from an execution backend."""

    def check_available(self) -> None: ...

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


def _run_apple_container(
    command: list[str],
    *,
    timeout: float,
    input: bytes | str | None = None,
    capture_output: bool = False,
    text: bool = False,
    stdout: int | None = None,
    stderr: int | None = None,
) -> subprocess.CompletedProcess[Any]:
    """Run Apple container CLI with the same robust capture/timeout behavior as Docker."""
    return _run_docker(
        command,
        timeout=timeout,
        input=input,
        capture_output=capture_output,
        text=text,
        stdout=stdout,
        stderr=stderr,
    )


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


def _docker_build_supports_load() -> bool:
    """Return whether ``docker build`` accepts ``--load`` in this environment."""
    try:
        completed = _run_docker(
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
        completed = _run_docker(
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
            _docker_build_command(
                dockerfile=str(dockerfile_path),
                tag=image,
                context=str(dockerfile_path.parent),
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


def check_apple_container_available() -> None:
    """Fail early with an actionable error when Apple container services are unavailable."""
    try:
        completed = _run_apple_container(
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


def check_sandbox_available(backend: SandboxBackend = DEFAULT_SANDBOX_BACKEND) -> None:
    """Check availability for the selected sandbox backend."""
    if backend == "docker":
        check_docker_available()
        return
    if backend == "apple":
        check_apple_container_available()
        return
    raise ValueError(f"Unsupported sandbox backend: {backend}")


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
            probe = _run_apple_container(
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

    dockerfile_path = _dockerfile_path(dockerfile)
    try:
        completed = _run_apple_container(
            [
                "container",
                "build",
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
            f"Apple container worker image build exceeded the {build_timeout:g}s timeout. "
            "Restart Apple container services and retry."
        ) from exc
    if completed.returncode != 0:
        raise SandboxError(
            f"Apple container worker image build failed with exit status {completed.returncode}. "
            "Verify it with 'container system status' and retry."
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


def _apple_image_exists(image: str) -> bool:
    try:
        probe = _run_apple_container(
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
        _run_apple_container(
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
        name_prefix: str = "nfind-search-",
    ) -> None:
        self.image = image
        self._dockerfile = Path(dockerfile)
        self.build_timeout = build_timeout
        self.name_prefix = name_prefix

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

    def derive_image(self, dockerfile_text: str, *, rebuild: bool = False) -> str:
        """Build (once, then cache) an image from ``dockerfile_text``; return its tag.

        The tag is content-hashed from the Dockerfile text, so identical derived images
        are reused across runs.
        """
        derived = _derived_image_tag(self.image, dockerfile_text)
        if not rebuild and _image_exists(derived):
            return derived
        with tempfile.TemporaryDirectory(prefix="nfind-deps-") as context:
            (Path(context) / "Dockerfile").write_text(dockerfile_text)
            try:
                completed = _run_docker(
                    _docker_build_command(dockerfile=None, tag=derived, context=context),
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


class AppleContainerSandbox:
    """Concrete :class:`Sandbox` backed by Apple's ``container`` CLI.

    Apple Containers use OCI images and Dockerfile-compatible builds, but their run
    flags are not a byte-for-byte Docker match. In particular, macOS 15 does not accept
    ``--network none``; macOS 26+ does, so nfind selects the strongest network flag the
    host supports. The CLI does not expose Docker's ``--pids-limit`` or
    ``--security-opt no-new-privileges`` options. Keep the supported hardening flags in
    one auditable method so the differences stay visible.
    """

    def __init__(
        self,
        image: str,
        *,
        dockerfile: str | Path = "Dockerfile.python",
        build_timeout: float = DEFAULT_BUILD_TIMEOUT,
        name_prefix: str = "nfind-search-",
    ) -> None:
        self.image = image
        self._dockerfile = Path(dockerfile)
        self.build_timeout = build_timeout
        self.name_prefix = name_prefix

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

    def derive_image(self, dockerfile_text: str, *, rebuild: bool = False) -> str:
        """Build (once, then cache) an image from ``dockerfile_text``; return its tag."""
        derived = _derived_image_tag(self.image, dockerfile_text)
        if not rebuild and _apple_image_exists(derived):
            return derived
        with tempfile.TemporaryDirectory(prefix="nfind-deps-") as context:
            (Path(context) / "Dockerfile").write_text(dockerfile_text)
            try:
                completed = _run_apple_container(
                    ["container", "build", "--tag", derived, context],
                    timeout=self.build_timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise SandboxError(
                    f"Building the derived Apple container image exceeded the "
                    f"{self.build_timeout:g}s timeout. Restart Apple container services and retry."
                ) from exc
        if completed.returncode != 0:
            raise SandboxError(
                f"Failed to build the derived Apple container image; "
                f"exit status {completed.returncode}."
            )
        return derived

    def _container_run_command(
        self, name: str, mounts: Sequence[Mount], limits: Limits
    ) -> list[str]:
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

    def run(self, stdin: bytes, *, mounts: list[Mount], limits: Limits) -> CompletedRun:
        """Run the image in a disposable Apple container with ``stdin`` piped in."""
        name = f"{self.name_prefix}{uuid.uuid4().hex}"
        command = self._container_run_command(name, mounts, limits)
        try:
            completed = _run_apple_container(
                command,
                input=stdin,
                capture_output=True,
                timeout=limits.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            _remove_apple_container(name)
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
    raise ValueError(f"Unsupported sandbox backend: {backend}")
