"""Optional process-wide deadline for one CLI invocation."""

from __future__ import annotations

import signal
from collections.abc import Callable


class CommandTimeoutError(TimeoutError):
    """Raised when the optional whole-command deadline expires."""


def arm_command_timeout(seconds: float | None) -> Callable[[], None]:
    """Arm a POSIX wall-clock deadline and return a function that restores state."""
    if seconds is None:
        return lambda: None
    if seconds <= 0:
        raise ValueError("--command-timeout must be positive.")
    if not hasattr(signal, "setitimer"):
        raise ValueError("--command-timeout is not supported on this platform.")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def expired(_signum: int, _frame: object) -> None:
        raise CommandTimeoutError(f"Command exceeded the {seconds:g}s whole-command timeout.")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)

    def cancel() -> None:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)

    return cancel
