"""End-to-end tests for Apple Containers on macOS versions with network isolation.

These tests exercise the real Apple container CLI, worker image, read-only root mount,
and ``--network none`` flag. They carry the ``integration`` marker and skip when Apple
Containers or a macOS 26+ host is unavailable.
"""

from __future__ import annotations

import platform
from pathlib import Path

import pytest

from nfind import constants, enumeration, execution, sandbox

pytestmark = pytest.mark.integration


def _apple_is_available() -> bool:
    if platform.machine().lower() not in {"arm64", "aarch64"}:
        return False
    version = platform.mac_ver()[0]
    try:
        major = int(version.split(".", maxsplit=1)[0])
    except (ValueError, IndexError):
        return False
    if major < 26:
        return False
    try:
        sandbox.check_apple_container_available()
    except sandbox.SandboxError:
        return False
    return True


@pytest.fixture(scope="module")
def apple_base_image() -> str:
    if not _apple_is_available():
        pytest.skip(
            "Apple Containers integration requires Apple silicon, macOS 26+, and a running service"
        )
    try:
        execution.build_worker_image(sandbox_backend="apple")
    except sandbox.SandboxError as exc:
        pytest.skip(f"Apple Containers worker image build failed: {exc}")
    return constants.DEFAULT_IMAGE


def test_apple_run_filter_end_to_end(apple_base_image: str, tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("needle")
    (tmp_path / "drop.log").write_text("hay")
    paths, _, mounts = enumeration.enumerate_roots([tmp_path])

    code = "def filter_paths(paths):\n    return [p for p in paths if open(p).read() == 'needle']"
    results = execution.run_filter(
        code,
        tmp_path,
        paths,
        sandbox_backend="apple",
        image=apple_base_image,
        mounts=mounts,
    )

    assert {record["path"] for record in results} == {str((tmp_path / "keep.txt").resolve())}


def test_apple_run_filter_network_is_disabled(apple_base_image: str, tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("x")
    paths, _, mounts = enumeration.enumerate_roots([tmp_path])
    code = (
        "def filter_paths(paths):\n"
        "    import socket\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=3)\n"
        "    return paths"
    )

    with pytest.raises(RuntimeError, match="Network is unreachable"):
        execution.run_filter(
            code,
            tmp_path,
            paths,
            sandbox_backend="apple",
            image=apple_base_image,
            mounts=mounts,
        )


def test_apple_run_filter_cannot_write_search_mount(apple_base_image: str, tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("original")
    paths, _, mounts = enumeration.enumerate_roots([tmp_path])
    code = "def filter_paths(paths):\n    open(paths[0], 'w').write('changed')\n    return paths"

    with pytest.raises(RuntimeError, match="Read-only file system"):
        execution.run_filter(
            code,
            tmp_path,
            paths,
            sandbox_backend="apple",
            image=apple_base_image,
            mounts=mounts,
        )
    assert target.read_text() == "original"
