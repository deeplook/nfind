"""End-to-end tests that build and run a real Docker container.

These are the deliberate, skip-guarded counterpart to the fast, Docker-free suite: they
exercise the actual ``docker build``/``docker run`` path (the hardened flags, the worker
protocol, and the timeout-kill mapping) against a live daemon. They are skipped wholesale
when Docker is unavailable, and can be deselected with ``-m 'not integration'``.

The LLM is intentionally bypassed -- a hand-written filter stands in for a generated one,
since code generation is not part of the sandbox boundary under test.
"""

from __future__ import annotations

import pytest

from nfind import backend, sandbox

pytestmark = pytest.mark.integration


def _docker_available() -> bool:
    try:
        sandbox.check_docker_available()
    except sandbox.SandboxError:
        return False
    return True


# Build the base image once for the module; also gates the whole module on Docker.
@pytest.fixture(scope="module")
def base_image() -> str:
    if not _docker_available():
        pytest.skip("Docker is not available")
    try:
        backend.build_worker_image()  # default Python base image
    except sandbox.SandboxError as exc:
        pytest.skip(f"Docker build failed: {exc}")
    return backend.DEFAULT_IMAGE


def test_run_filter_end_to_end(base_image, tmp_path):
    (tmp_path / "keep.txt").write_text("x")
    (tmp_path / "drop.log").write_text("x")
    container_paths, _ = backend.enumerate_paths(tmp_path)

    code = "def filter_paths(paths):\n    return [p for p in paths if p.endswith('.txt')]"
    results = backend.run_filter(code, tmp_path, container_paths, image=base_image)

    paths = {record["path"] for record in results}
    assert paths == {"/data/keep.txt"}


def test_run_filter_can_read_mounted_file_contents(base_image, tmp_path):
    tmp_path.chmod(0o755)
    (tmp_path / "needle.txt").write_text("needle")
    (tmp_path / "hay.txt").write_text("hay")
    for f in tmp_path.iterdir():
        f.chmod(0o644)
    container_paths, _ = backend.enumerate_paths(tmp_path)

    code = "def filter_paths(paths):\n    return [p for p in paths if open(p).read() == 'needle']"
    results = backend.run_filter(code, tmp_path, container_paths, image=base_image)

    assert {record["path"] for record in results} == {"/data/needle.txt"}


def test_run_filter_network_is_disabled(base_image, tmp_path):
    # The container runs with --network none; an outbound connection must fail, proving
    # the generated code cannot exfiltrate over the network.
    (tmp_path / "a.txt").write_text("x")
    container_paths, _ = backend.enumerate_paths(tmp_path)

    code = (
        "def filter_paths(paths):\n"
        "    import socket\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=3)\n"
        "    return paths\n"
    )
    with pytest.raises(RuntimeError):
        backend.run_filter(code, tmp_path, container_paths, image=base_image)


def test_run_filter_times_out(base_image, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    container_paths, _ = backend.enumerate_paths(tmp_path)

    code = "def filter_paths(paths):\n    import time\n    time.sleep(30)\n    return paths"
    with pytest.raises(TimeoutError):
        backend.run_filter(code, tmp_path, container_paths, image=base_image, timeout=2)
