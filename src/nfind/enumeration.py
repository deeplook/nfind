"""Host-side search-root enumeration.

This module shapes the path list before generated code sees it: search roots are
resolved, ignored directories are pruned, explicit exclude globs are applied, and
multiple roots are namespaced under distinct container mount points.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from .constants import DEFAULT_IGNORES
from .sandbox import Mount


def _matches_any(name: str, relative_posix: str, patterns: Sequence[str]) -> bool:
    """True when ``name`` or its root-relative POSIX path matches any glob."""
    return any(
        fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relative_posix, pattern)
        for pattern in patterns
    )


def enumerate_paths(
    search_root: Path,
    *,
    exclude: Sequence[str] = (),
    max_depth: int | None = None,
    use_default_ignores: bool = True,
    container_root: str = "/data",
) -> tuple[list[str], dict[str, str]]:
    """Return container paths and a container-to-host result mapping.

    ``exclude`` is a list of glob patterns matched against each entry's name *and* its
    path relative to the search root (POSIX form); a matching directory is pruned --
    skipped from the results and not descended into. When ``use_default_ignores`` is true
    (the default), the common VCS/dependency/cache names in :data:`DEFAULT_IGNORES` are
    excluded too. ``max_depth`` bounds how deep below the root to descend -- a direct child
    is depth 1 -- and ``None`` (the default) means unlimited. ``container_root`` is the
    in-container mount point the relative entries hang off (``/data`` for a single root;
    :func:`enumerate_roots` passes ``/data/0``, ``/data/1``, ... to keep multiple roots
    from colliding).
    """
    root = search_root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Search root is not a directory: {root}")
    if max_depth is not None and max_depth < 1:
        raise ValueError("max_depth must be at least 1")

    patterns = [*exclude]
    if use_default_ignores:
        patterns += sorted(DEFAULT_IGNORES)

    container_paths: list[str] = []
    host_by_container: dict[str, str] = {}
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        depth = len(current_path.relative_to(root).parts)

        # Prune excluded directories in place so os.walk neither lists nor descends them.
        kept: list[str] = []
        for name in directories:
            relative_posix = (current_path / name).relative_to(root).as_posix()
            if not _matches_any(name, relative_posix, patterns):
                kept.append(name)
        directories[:] = kept

        for name in [*directories, *files]:
            host_path = current_path / name
            relative = host_path.relative_to(root)
            if name in files and _matches_any(name, relative.as_posix(), patterns):
                continue
            container_path = str(PurePosixPath(container_root, *relative.parts))
            container_paths.append(container_path)
            host_by_container[container_path] = str(host_path)

        # Stop descending once the next level would exceed max_depth; entries at the
        # current level (including directories) have already been recorded above.
        if max_depth is not None and depth + 1 >= max_depth:
            directories[:] = []
    return container_paths, host_by_container


def _normalize_roots(path: str | Path | Sequence[str | Path]) -> list[Path]:
    """Resolve one-or-many search paths to a de-duplicated list of existing roots."""
    items: Sequence[str | Path]
    items = [path] if isinstance(path, (str, Path)) else list(path)
    if not items:
        items = ["."]
    roots: list[Path] = []
    seen: set[Path] = set()
    for item in items:
        root = Path(item).expanduser().resolve(strict=True)
        if root not in seen:
            seen.add(root)
            roots.append(root)
    return roots


def _enumerate_root(
    root: Path,
    container_root: str,
    *,
    exclude: Sequence[str],
    max_depth: int | None,
    use_default_ignores: bool,
) -> tuple[list[str], dict[str, str], Mount]:
    """Enumerate one search root (a directory tree or a single file) and its mount.

    A directory is walked as usual and bind-mounted at ``container_root``. A file root is
    a degenerate enumeration -- a single entry mounted as a read-only file at
    ``container_root/<name>`` -- so the filter receives exactly that path. ``exclude``,
    ``max_depth``, and ``use_default_ignores`` only shape directory walks; they are no-ops
    for a file root.
    """
    if root.is_dir():
        paths, mapping = enumerate_paths(
            root,
            exclude=exclude,
            max_depth=max_depth,
            use_default_ignores=use_default_ignores,
            container_root=container_root,
        )
        return paths, mapping, Mount(root, container_root, read_only=True)

    container_path = str(PurePosixPath(container_root, root.name))
    mount = Mount(root, container_path, read_only=True)
    return [container_path], {container_path: str(root)}, mount


def enumerate_roots(
    roots: Sequence[Path],
    *,
    exclude: Sequence[str] = (),
    max_depth: int | None = None,
    use_default_ignores: bool = True,
) -> tuple[list[str], dict[str, str], list[Mount]]:
    """Enumerate one or more search roots (directories or files) and the mounts for them.

    A single root hangs off ``/data``; multiple roots are namespaced under ``/data/0``,
    ``/data/1``, ... so identically named entries from different roots don't collide. Each
    root may be a directory (walked) or a file (a single mounted path).
    """
    if not roots:
        raise ValueError("at least one search root is required")

    container_paths: list[str] = []
    host_by_container: dict[str, str] = {}
    mounts: list[Mount] = []
    for index, root in enumerate(roots):
        container_root = "/data" if len(roots) == 1 else f"/data/{index}"
        paths, mapping, mount = _enumerate_root(
            root,
            container_root,
            exclude=exclude,
            max_depth=max_depth,
            use_default_ignores=use_default_ignores,
        )
        container_paths.extend(paths)
        host_by_container.update(mapping)
        mounts.append(mount)
    return container_paths, host_by_container, mounts
