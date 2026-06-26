"""Host-side search-root enumeration.

This module shapes the path list before generated code sees it: search roots are
resolved, ignored directories are pruned, explicit exclude globs are applied, and each
root is bind-mounted at its own host path when safe (so the filter sees real paths) or
under a neutral, namespaced container mount point otherwise.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from .constants import DEFAULT_IGNORES
from .sandbox import Mount

# Container top-level directories the worker image needs intact. Mounting a search root
# *at* one of these (e.g. searching "/usr") would shadow it and break the worker, so such
# roots fall back to neutral ``/data`` mountpoints rather than identity mounting.
_RESERVED_CONTAINER_DIRS = frozenset(
    {
        "app",
        "bin",
        "boot",
        "dev",
        "etc",
        "home",
        "lib",
        "lib32",
        "lib64",
        "libx32",
        "media",
        "mnt",
        "opt",
        "proc",
        "root",
        "run",
        "sbin",
        "srv",
        "sys",
        "tmp",
        "usr",
        "var",
    }
)


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
    in-container mount point the relative entries hang off; :func:`enumerate_roots` passes
    the root's own host path when identity mounting, or ``/data``/``/data/0``/``/data/1``...
    when falling back to neutral namespaced mountpoints.
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
    identity: bool,
    exclude: Sequence[str],
    max_depth: int | None,
    use_default_ignores: bool,
) -> tuple[list[str], dict[str, str], Mount]:
    """Enumerate one search root (a directory tree or a single file) and its mount.

    A directory is walked as usual and bind-mounted at ``container_root``. A file root is
    a degenerate enumeration -- a single mounted read-only file -- whose in-container path
    is ``container_root`` itself under identity mounting (``container_root`` already is the
    file's host path) or ``container_root/<name>`` under the namespaced fallback. ``exclude``,
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

    container_path = container_root if identity else str(PurePosixPath(container_root, root.name))
    mount = Mount(root, container_path, read_only=True)
    return [container_path], {container_path: str(root)}, mount


def _is_filesystem_root(path: Path) -> bool:
    """True when ``path`` is a filesystem root (its own parent), e.g. ``/``."""
    return path.parent == path


def _shadows_container_dir(path: Path) -> bool:
    """True when mounting ``path`` at its own location would hide a needed container dir.

    Only a direct child of the filesystem root whose name matches a reserved top-level
    container directory is a problem; deeper paths merely create fresh leaf mountpoints.
    """
    return _is_filesystem_root(path.parent) and path.name in _RESERVED_CONTAINER_DIRS


def _roots_overlap(roots: Sequence[Path]) -> bool:
    """True when any root equals or nests inside another (their trees would collide)."""
    for index, first in enumerate(roots):
        for second in roots[index + 1 :]:
            if first.is_relative_to(second) or second.is_relative_to(first):
                return True
    return False


def _can_identity_mount(roots: Sequence[Path]) -> bool:
    """Whether every root can be mounted at its own host path (container path == host path).

    Identity mounting lets generated filters reason about real paths -- and makes
    natural "files under /some/dir" phrasing work -- while collapsing the container/host
    translation to identity. It applies only on POSIX hosts: the Linux container needs a
    POSIX mount target, so a Windows path like ``C:\\Users\\me`` can never be an identity
    mount point. It is also unsafe when a root is the filesystem root, would shadow a
    reserved container directory, or overlaps another root. Any such case forces the whole
    set back to neutral ``/data`` mountpoints.
    """
    return (
        bool(roots)
        and os.name == "posix"
        and not any(_is_filesystem_root(root) for root in roots)
        and not any(_shadows_container_dir(root) for root in roots)
        and not _roots_overlap(roots)
    )


def _container_root_for(root: Path, *, identity: bool, index: int, count: int) -> str:
    """Pick the in-container mount point for one root (identity host path or namespaced)."""
    if identity:
        return str(root)
    return "/data" if count == 1 else f"/data/{index}"


def enumerate_roots(
    roots: Sequence[Path],
    *,
    exclude: Sequence[str] = (),
    max_depth: int | None = None,
    use_default_ignores: bool = True,
) -> tuple[list[str], dict[str, str], list[Mount]]:
    """Enumerate one or more search roots (directories or files) and the mounts for them.

    When safe (see :func:`_can_identity_mount`), each root is bind-mounted at its own host
    path so the in-container paths the filter sees are identical to the host paths and the
    container-to-host mapping is the identity. Otherwise -- a root is ``/``, would shadow a
    reserved container directory, or roots overlap -- the set falls back to neutral
    mountpoints: a single root hangs off ``/data`` and multiple roots are namespaced under
    ``/data/0``, ``/data/1``, ... so identically named entries don't collide. Each root may
    be a directory (walked) or a file (a single mounted path).
    """
    if not roots:
        raise ValueError("at least one search root is required")

    # Resolve up front so the identity decision, mount targets, and the host mapping all
    # agree (``enumerate_paths`` resolves too, so container paths are real-path based).
    roots = [root.expanduser().resolve(strict=True) for root in roots]
    identity = _can_identity_mount(roots)
    container_paths: list[str] = []
    host_by_container: dict[str, str] = {}
    mounts: list[Mount] = []
    for index, root in enumerate(roots):
        container_root = _container_root_for(root, identity=identity, index=index, count=len(roots))
        paths, mapping, mount = _enumerate_root(
            root,
            container_root,
            identity=identity,
            exclude=exclude,
            max_depth=max_depth,
            use_default_ignores=use_default_ignores,
        )
        container_paths.extend(paths)
        host_by_container.update(mapping)
        mounts.append(mount)
    return container_paths, host_by_container, mounts
