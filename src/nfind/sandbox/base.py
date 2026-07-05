"""Shared sandbox core: value types, errors, the CLI runner, and the backend base class.

This module owns everything that is *not* specific to one container CLI: the request and
response value types, the error hierarchy, the :class:`Sandbox` ``Protocol``, the robust
subprocess runner (:func:`_run_cli`) every backend shares, and :class:`_CliSandbox`, the
template base class that implements the identical ``run``/``derive_image`` mechanics so
each backend only supplies its own hardened command set.

The security-relevant flag sets deliberately do *not* live here: each backend keeps its
container-run invocation in one auditable method (see :mod:`nfind.sandbox.docker` and
:mod:`nfind.sandbox.apple`), so adding a backend (e.g. Podman) is a small subclass rather
than a copy of this logic.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import signal
import subprocess
import sys
import tempfile
import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..constants import (
    DEFAULT_BUILD_TIMEOUT,
    DEFAULT_CPUS,
    DEFAULT_MEMORY,
    DEFAULT_PIDS_LIMIT,
    DEFAULT_TIMEOUT,
)


@dataclass(frozen=True)
class Mount:
    """A host directory bound into the container at ``target``."""

    source: Path
    target: str
    read_only: bool = True


@dataclass(frozen=True)
class Limits:
    """Resource and output limits for a single sandboxed run."""

    memory: str = DEFAULT_MEMORY
    cpus: float = DEFAULT_CPUS
    pids: int = DEFAULT_PIDS_LIMIT
    timeout: float = DEFAULT_TIMEOUT
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


def dockerfile_path(name: str = "Dockerfile.python") -> Path:
    """Locate a Dockerfile packaged next to the sandbox package."""
    return Path(__file__).parent.parent / name


def _docker_error_detail(completed: subprocess.CompletedProcess[str]) -> str:
    detail = (completed.stderr or completed.stdout or "unknown error").strip()
    return detail[-500:]


def _run_cli(
    command: list[str],
    *,
    timeout: float,
    input: bytes | str | None = None,
    capture_output: bool = False,
    text: bool = False,
    stdout: int | None = None,
    stderr: int | None = None,
) -> subprocess.CompletedProcess[Any]:
    """Run a container CLI and kill its whole CLI/plugin process group on timeout.

    Backend-agnostic: ``docker`` and Apple's ``container`` share the same daemonizing
    plugin behavior, so both go through this one runner.
    """
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
    except BaseException:
        # A whole-command deadline or user interrupt can arrive while communicate()
        # is blocked. Do not leave the CLI process (or its process group) behind.
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)
        if captured_stdout is not None:
            captured_stdout.close()
        if captured_stderr is not None:
            captured_stderr.close()
        raise

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


def derived_image_tag(image: str, dockerfile_text: str) -> str:
    """Stable, content-hashed tag for an image derived from ``dockerfile_text``."""
    repository = image.split(":", 1)[0]
    digest = hashlib.sha256(dockerfile_text.encode()).hexdigest()[:12]
    return f"{repository}:deps-{digest}"


class _CliSandbox(ABC):
    """Template base for CLI-backed sandboxes.

    Holds the identical construction, ``run`` and ``derive_image`` mechanics shared by
    every ``docker``-style backend. Subclasses supply only what genuinely differs: the
    availability check, the base-image build, the image-existence probe, the derived-
    image build command, the container-removal call, and -- the security-critical part --
    the hardened run command (:meth:`_build_run_command`, kept as one auditable method per
    backend). Adding a backend such as Podman means a small subclass, not a copy of this
    logic.
    """

    #: Image noun and restart hint interpolated into ``derive_image`` error messages.
    _derived_noun: str = "derived sandbox image"
    _restart_hint: str = "Restart Docker and retry."

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

    # --- hooks each backend must implement ----------------------------------

    @abstractmethod
    def check_available(self) -> None:
        """Verify the backend CLI/daemon is reachable."""

    @abstractmethod
    def ensure_image(self, *, rebuild: bool = False) -> None:
        """Build the base image when absent, or unconditionally when requested."""

    @abstractmethod
    def _image_present(self, image: str) -> bool:
        """Return whether ``image`` already exists locally."""

    @abstractmethod
    def _build_derived_command(self, derived: str, context: str) -> list[str]:
        """Return the CLI command that builds ``derived`` from ``context``."""

    @abstractmethod
    def _build_run_command(self, name: str, mounts: Sequence[Mount], limits: Limits) -> list[str]:
        """Assemble the single, auditable hardened container-run invocation."""

    @abstractmethod
    def _remove(self, name: str) -> None:
        """Force-remove a container by ``name``, ignoring lifecycle errors."""

    # --- mechanics shared across backends -----------------------------------

    def derive_image(self, dockerfile_text: str, *, rebuild: bool = False) -> str:
        """Build (once, then cache) an image from ``dockerfile_text``; return its tag.

        The tag is content-hashed from the Dockerfile text, so identical derived images
        are reused across runs.
        """
        derived = derived_image_tag(self.image, dockerfile_text)
        if not rebuild and self._image_present(derived):
            return derived
        # Frame the raw build output that streams to stderr as an expected one-off step.
        print("nfind: building the sandbox image with requested packages...", file=sys.stderr)
        with tempfile.TemporaryDirectory(prefix="nfind-deps-") as context:
            (Path(context) / "Dockerfile").write_text(dockerfile_text)
            try:
                completed = _run_cli(
                    self._build_derived_command(derived, context),
                    timeout=self.build_timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise SandboxError(
                    f"Building the {self._derived_noun} exceeded the "
                    f"{self.build_timeout:g}s timeout. {self._restart_hint}"
                ) from exc
        if completed.returncode != 0:
            raise SandboxError(
                f"Failed to build the {self._derived_noun}; exit status {completed.returncode}."
            )
        return derived

    def run(self, stdin: bytes, *, mounts: list[Mount], limits: Limits) -> CompletedRun:
        """Run the image in a disposable, hardened container with ``stdin`` piped in."""
        name = f"{self.name_prefix}{uuid.uuid4().hex}"
        command = self._build_run_command(name, mounts, limits)
        try:
            completed = _run_cli(
                command,
                input=stdin,
                capture_output=True,
                timeout=limits.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self._remove(name)
            raise SandboxTimeout(f"Sandbox run exceeded the {limits.timeout:g}s timeout.") from exc
        except BaseException:
            self._remove(name)
            raise

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
