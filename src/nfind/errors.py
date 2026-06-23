"""Exception types shared across nfind's host-side modules.

The Docker lifecycle errors are the generic :class:`~nfind.sandbox.SandboxError`
hierarchy, re-exported here under their historical ``Docker*`` names so existing
``except DockerUnavailableError`` call sites (and the public API) keep working while the
sandbox component owns the canonical definitions.
"""

from __future__ import annotations

from .sandbox import SandboxError, SandboxUnavailable

# Backwards-compatible aliases: the canonical types live in nfind.sandbox.
DockerError = SandboxError
DockerUnavailableError = SandboxUnavailable


class DependencyError(RuntimeError):
    """Raised when a filter needs packages that were not approved for install."""
