"""Test doubles for the sandbox boundary, so most tests need no Docker."""

from __future__ import annotations

from nfind.sandbox import CompletedRun, Limits, Mount


class FakeSandbox:
    """In-memory :class:`~nfind.sandbox.Sandbox` for tests.

    ``run`` returns a canned :class:`CompletedRun` (or raises a configured error);
    ``ensure_image`` / ``derive_image`` record their calls and no-op.
    """

    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        run_error: BaseException | None = None,
        derived: str = "fake-image:deps",
        derive_error: BaseException | None = None,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._returncode = returncode
        self._run_error = run_error
        self._derived = derived
        self._derive_error = derive_error
        self.ensure_calls: list[bool] = []
        self.check_calls = 0
        self.derive_calls: list[str] = []
        self.runs: list[tuple[bytes, list[Mount], Limits]] = []

    def check_available(self) -> None:
        self.check_calls += 1

    def ensure_image(self, *, rebuild: bool = False) -> None:
        self.ensure_calls.append(rebuild)

    def derive_image(self, dockerfile_text: str, *, rebuild: bool = False) -> str:
        self.derive_calls.append(dockerfile_text)
        if self._derive_error is not None:
            raise self._derive_error
        return self._derived

    def run(self, stdin: bytes, *, mounts: list[Mount], limits: Limits) -> CompletedRun:
        self.runs.append((stdin, mounts, limits))
        if self._run_error is not None:
            raise self._run_error
        return CompletedRun(stdout=self._stdout, stderr=self._stderr, returncode=self._returncode)
