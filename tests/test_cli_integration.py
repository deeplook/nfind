"""End-to-end CLI/config tests against real sandbox backends.

These tests deliberately use ``nfind --run`` so the LLM is bypassed while the public
CLI, config-file defaulting, image preparation, and container run path are exercised
against a live Docker daemon, Apple ``container`` service, or nerdctl/containerd runtime.
Each backend is skipped when its runtime is unavailable, so on a typical dev box only the
locally installed backends run; the nerdctl path is exercised on the Linux CI job that
installs containerd.
"""

from __future__ import annotations

import signal
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nfind import cli, constants, sandbox

pytestmark = pytest.mark.integration


def _backend_available(name: sandbox.SandboxBackend) -> bool:
    try:
        sandbox.check_sandbox_available(name)
    except sandbox.SandboxError:
        return False
    return name != "docker" or sandbox.docker_supports_linux_containers()


@pytest.fixture(params=["docker", "apple", "nerdctl"])
def sandbox_backend(request: pytest.FixtureRequest) -> Iterator[sandbox.SandboxBackend]:
    name = request.param
    if name not in {"docker", "apple", "nerdctl"}:
        raise AssertionError(f"unexpected sandbox backend fixture param: {name}")
    if not _backend_available(name):
        pytest.skip(f"{name} sandbox backend is not available")
    yield name


@pytest.fixture
def sample_tree(tmp_path: Path) -> Path:
    (tmp_path / "keep.txt").write_text("keep")
    (tmp_path / "drop.log").write_text("drop")
    return tmp_path


@pytest.fixture
def saved_filter(tmp_path: Path) -> Path:
    script = tmp_path / "filter.py"
    script.write_text(
        "def filter_paths(paths):\n"
        "    return [p for p in paths if p.endswith('/keep.txt') or p.endswith('keep.txt')]\n"
    )
    return script


def _runner() -> CliRunner:
    return CliRunner()


def _assert_successful_cli_run(
    result,
    *,
    backend_name: sandbox.SandboxBackend,
    expected: Path,
) -> None:
    assert result.exit_code == 0, result.output
    assert str(expected) in result.output
    assert "drop.log" not in result.output
    # Each experimental backend prints its own warning; docker prints none.
    expected_warnings = {
        "apple": "Apple Containers sandbox is experimental",
        "nerdctl": "nerdctl sandbox is experimental",
    }
    for name, marker in expected_warnings.items():
        if backend_name == name:
            assert marker in result.output
        else:
            assert marker not in result.output


def test_cli_run_exercises_real_backend_with_sandbox_resource_flags(
    sandbox_backend: sandbox.SandboxBackend,
    saved_filter: Path,
    sample_tree: Path,
) -> None:
    cpus = "1" if sandbox_backend == "apple" else "0.5"
    result = _runner().invoke(
        cli.app,
        [
            "--run",
            str(saved_filter),
            str(sample_tree),
            "--sandbox",
            sandbox_backend,
            "--image",
            constants.DEFAULT_IMAGE,
            "--timeout",
            "10",
            "--memory",
            "256m",
            "--cpus",
            cpus,
            "--pids-limit",
            "64",
            "--build-timeout",
            "120",
            "--rebuild",
        ],
    )

    _assert_successful_cli_run(
        result,
        backend_name=sandbox_backend,
        expected=sample_tree / "keep.txt",
    )


def test_config_defaults_exercise_real_backend_sandbox_resource_flags(
    sandbox_backend: sandbox.SandboxBackend,
    saved_filter: Path,
    sample_tree: Path,
    tmp_path: Path,
) -> None:
    cpus = "1" if sandbox_backend == "apple" else "0.5"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'sandbox = "{sandbox_backend}"\n'
        f'image = "{constants.DEFAULT_IMAGE}"\n'
        "timeout = 10\n"
        'memory = "256m"\n'
        f"cpus = {cpus}\n"
        "pids-limit = 64\n"
        "build-timeout = 120\n"
    )

    result = _runner().invoke(
        cli.app,
        ["--config", str(cfg), "--run", str(saved_filter), str(sample_tree)],
    )

    _assert_successful_cli_run(
        result,
        backend_name=sandbox_backend,
        expected=sample_tree / "keep.txt",
    )


def test_cli_timeout_overrides_config_for_real_backend(
    sandbox_backend: sandbox.SandboxBackend,
    sample_tree: Path,
    tmp_path: Path,
) -> None:
    script = tmp_path / "slow_filter.py"
    script.write_text(
        "def filter_paths(paths):\n"
        "    import time\n"
        "    time.sleep(2)\n"
        "    return [p for p in paths if p.endswith('keep.txt')]\n"
    )
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'sandbox = "{sandbox_backend}"\n'
        f'image = "{constants.DEFAULT_IMAGE}"\n'
        "timeout = 1\n"
        'memory = "256m"\n'
        "cpus = 1\n"
        "pids-limit = 64\n"
        "build-timeout = 120\n"
    )

    result = _runner().invoke(
        cli.app,
        [
            "--config",
            str(cfg),
            "--run",
            str(script),
            str(sample_tree),
            "--timeout",
            "5",
        ],
    )

    _assert_successful_cli_run(
        result,
        backend_name=sandbox_backend,
        expected=sample_tree / "keep.txt",
    )


def test_docker_cli_memory_limit_failure(sample_tree: Path, tmp_path: Path) -> None:
    if not _backend_available("docker"):
        pytest.skip("docker sandbox backend is not available")

    script = tmp_path / "memory_hog_filter.py"
    script.write_text(
        "def filter_paths(paths):\n"
        "    _buffers = [b'x' * (1024 * 1024) for _ in range(512)]\n"
        "    return paths\n"
    )

    result = _runner().invoke(
        cli.app,
        [
            "--run",
            str(script),
            str(sample_tree),
            "--sandbox",
            "docker",
            "--image",
            constants.DEFAULT_IMAGE,
            "--timeout",
            "15",
            "--memory",
            "96m",
        ],
    )

    assert result.exit_code == 1
    assert "Generated filter failed" in result.output


@pytest.mark.skipif(
    not hasattr(signal, "setitimer"), reason="whole-command timeout needs a POSIX timer"
)
def test_docker_cli_whole_command_timeout_aborts_and_cleans_up(
    saved_filter: Path,
    sample_tree: Path,
    tmp_path: Path,
) -> None:
    if not _backend_available("docker"):
        pytest.skip("docker sandbox backend is not available")

    # Prime the image with a fast run so the deadline below fires during the container
    # run rather than an image build — that is the path whose cleanup we want to prove.
    warmup = _runner().invoke(
        cli.app,
        [
            "--run",
            str(saved_filter),
            str(sample_tree),
            "--sandbox",
            "docker",
            "--image",
            constants.DEFAULT_IMAGE,
            "--timeout",
            "30",
        ],
    )
    assert warmup.exit_code == 0, warmup.output

    slow = tmp_path / "slow_filter.py"
    slow.write_text(
        "def filter_paths(paths):\n    import time\n    time.sleep(30)\n    return paths\n"
    )

    result = _runner().invoke(
        cli.app,
        [
            "--run",
            str(slow),
            str(sample_tree),
            "--sandbox",
            "docker",
            "--image",
            constants.DEFAULT_IMAGE,
            # Filter timeout stays high so the *whole-command* deadline wins the race.
            "--timeout",
            "60",
            "--command-timeout",
            "3",
        ],
    )

    assert result.exit_code == 1
    assert "whole-command timeout" in result.output

    # The interrupted run must not leave its worker container behind.
    running = subprocess.run(
        ["docker", "ps", "-q", "--filter", "name=nfind-search-"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert running.stdout.strip() == "", f"leftover container: {running.stdout!r}"


def test_apple_cli_rejects_fractional_cpu_before_running(
    saved_filter: Path,
    sample_tree: Path,
) -> None:
    if not _backend_available("apple"):
        pytest.skip("apple sandbox backend is not available")

    result = _runner().invoke(
        cli.app,
        [
            "--run",
            str(saved_filter),
            str(sample_tree),
            "--sandbox",
            "apple",
            "--cpus",
            "0.5",
        ],
    )

    assert result.exit_code == 1
    assert "Apple Containers sandbox is experimental" in result.output
    assert "Apple Containers requires --cpus to be a whole number" in result.output
