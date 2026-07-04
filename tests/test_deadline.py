"""Unit tests for the optional whole-command deadline.

These exercise :mod:`nfind.deadline` directly with tiny (or immediately cancelled)
timers, so they need no sandbox and never block on real work. The POSIX-timer cases
are skipped where ``signal.setitimer`` is unavailable.
"""

from __future__ import annotations

import signal
import time

import pytest

from nfind.deadline import CommandTimeoutError, arm_command_timeout

posix_only = pytest.mark.skipif(
    not hasattr(signal, "setitimer"), reason="POSIX interval timer required"
)


def test_arm_none_returns_noop_canceller() -> None:
    cancel = arm_command_timeout(None)
    cancel()  # must be safe to call and do nothing


@pytest.mark.parametrize("bad", [0, -1.0])
def test_arm_rejects_non_positive(bad: float) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        arm_command_timeout(bad)


@posix_only
def test_cancel_restores_prior_signal_state() -> None:
    def sentinel(_signum: int, _frame: object) -> None:  # pragma: no cover - never fires
        raise AssertionError("prior handler should not run")

    signal.signal(signal.SIGALRM, sentinel)
    signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        cancel = arm_command_timeout(5.0)
        # While armed, our own handler and a live timer replace the prior state.
        assert signal.getsignal(signal.SIGALRM) is not sentinel
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 0

        cancel()

        # Cancelling must put the previous handler back and disarm the timer.
        assert signal.getsignal(signal.SIGALRM) is sentinel
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    finally:
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.setitimer(signal.ITIMER_REAL, 0)


@posix_only
def test_expired_timer_raises_command_timeout_error() -> None:
    cancel = arm_command_timeout(0.01)
    try:
        with pytest.raises(CommandTimeoutError, match="whole-command timeout"):
            time.sleep(0.5)
    finally:
        cancel()
