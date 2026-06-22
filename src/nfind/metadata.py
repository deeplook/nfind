"""Host-side macOS extended-attribute collection for ``--macos-meta``.

These attributes live on the host and do not reliably survive Docker's file-sharing
layer into the Linux container, so they are read here and passed into the sandbox
alongside the paths.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import plistlib
import sys
from collections.abc import Callable
from typing import Any

_XATTR_TAGS = "com.apple.metadata:_kMDItemUserTags"
_XATTR_WHERE_FROMS = "com.apple.metadata:kMDItemWhereFroms"
_XATTR_QUARANTINE = "com.apple.quarantine"
_XATTR_NOFOLLOW = 0x0001  # macOS getxattr option: do not follow symlinks

_libc_getxattr: Callable[..., int] | None = None


def _getxattr(path: str, name: str) -> bytes | None:
    """Read a single macOS extended attribute, or None if it is absent.

    Uses libc ``getxattr`` directly (CPython's ``os.getxattr`` is Linux-only) and
    does not follow symlinks, matching the symlink-free host enumeration.
    """
    global _libc_getxattr
    if _libc_getxattr is None:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        libc.getxattr.restype = ctypes.c_ssize_t
        libc.getxattr.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        ]
        _libc_getxattr = libc.getxattr

    path_bytes = os.fsencode(path)
    name_bytes = name.encode()
    size = _libc_getxattr(path_bytes, name_bytes, None, 0, 0, _XATTR_NOFOLLOW)
    if size < 0:
        return None
    if size == 0:
        return b""
    buffer = ctypes.create_string_buffer(size)
    read = _libc_getxattr(path_bytes, name_bytes, buffer, size, 0, _XATTR_NOFOLLOW)
    if read < 0:
        return None
    return buffer.raw[:read]


def _plist_strings(raw: bytes | None) -> list[str]:
    """Decode a binary-plist array of strings, tolerating malformed values."""
    if not raw:
        return []
    try:
        values = plistlib.loads(raw)
    except Exception:  # noqa: BLE001 - any decode failure means "no usable value"
        return []
    return [v for v in values if isinstance(v, str)] if isinstance(values, list) else []


def collect_macos_metadata(host_by_container: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Read selected macOS attributes per path, keyed by container path.

    Returns a mapping from container path to a metadata dict for the paths that have
    any. Each value may contain "tags" (Finder tag names), "quarantined" (True when
    the file carries a download-quarantine flag), and "where_froms" (source URLs).
    Returns an empty mapping on non-macOS hosts so callers degrade gracefully.
    """
    if sys.platform != "darwin":
        return {}
    metadata: dict[str, dict[str, Any]] = {}
    for container_path, host_path in host_by_container.items():
        entry: dict[str, Any] = {}
        # Finder tags are stored as "Name" or "Name\n<color-index>"; keep the name.
        tags = [
            value.split("\n", 1)[0] for value in _plist_strings(_getxattr(host_path, _XATTR_TAGS))
        ]
        if tags:
            entry["tags"] = tags
        if _getxattr(host_path, _XATTR_QUARANTINE) is not None:
            entry["quarantined"] = True
        where_froms = _plist_strings(_getxattr(host_path, _XATTR_WHERE_FROMS))
        if where_froms:
            entry["where_froms"] = where_froms
        if entry:
            metadata[container_path] = entry
    return metadata
