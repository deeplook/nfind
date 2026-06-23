"""Best-effort cache of which API endpoint a model needs (chat vs. responses).

Most models answer on ``/chat/completions``; OpenAI reasoning/codex models are served
only on ``/responses`` and reject the former with a 404 (see
``generation._is_responses_only``).
Discovering that costs one throwaway request per process. Caching the ``"responses"``
verdict -- keyed by the full ``provider/model`` selector -- lets later runs skip the probe.

The cache lives as JSON in nfind's cache directory as ``model-endpoints.json`` (or
``$NFIND_ENDPOINT_CACHE`` when set; see :mod:`nfind.paths`). It is purely an optimisation:
every read and write is best-effort and swallows I/O errors, since the verdict can always
be re-derived by probing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .paths import user_dir


def _cache_path() -> Path:
    """Location of the endpoint cache file."""
    override = os.environ.get("NFIND_ENDPOINT_CACHE")
    if override:
        return Path(override)
    return user_dir("cache") / "model-endpoints.json"


def _read() -> dict[str, str]:
    try:
        data = json.loads(_cache_path().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def get_endpoint(model: str) -> str | None:
    """Return the cached endpoint for ``model`` (the full selector), or ``None``."""
    return _read().get(model)


def set_endpoint(model: str, endpoint: str) -> None:
    """Record that ``model`` needs ``endpoint``; best-effort, never raises."""
    data = _read()
    if data.get(model) == endpoint:
        return
    data[model] = endpoint
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    except OSError:
        pass  # the cache is an optimisation; losing a write just means a future re-probe
