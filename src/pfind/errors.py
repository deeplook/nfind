"""Exception types shared across pfind's host-side modules."""

from __future__ import annotations


class DockerError(RuntimeError):
    """Base class for actionable Docker lifecycle failures."""


class DockerUnavailableError(DockerError):
    """Raised when the Docker CLI or daemon cannot be reached."""


class DependencyError(RuntimeError):
    """Raised when a filter needs packages that were not approved for install."""
