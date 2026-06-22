"""Pure Docker-mechanics tests targeting the DockerSandbox layer.

These exercise the hardened flag set, image build/derive, and the timeout/output-size
mapping directly against :mod:`pfind.sandbox`, patching ``_run_docker`` so they need no
running Docker daemon.
"""

import subprocess
from unittest.mock import Mock, patch

import pytest

from pfind import sandbox
from pfind.sandbox import DockerSandbox, Limits, Mount


def test_docker_error_aliases_map_to_sandbox_hierarchy():
    # Existing `except DockerUnavailableError` / `except DockerError` call sites must keep
    # catching what the sandbox raises, so the aliases are the same objects.
    from pfind.errors import DockerError, DockerUnavailableError

    assert DockerUnavailableError is sandbox.SandboxUnavailable
    assert DockerError is sandbox.SandboxError
    assert issubclass(sandbox.SandboxUnavailable, DockerError)


# --- check_docker_available -----------------------------------------------------


def test_docker_check_accepts_empty_container_list():
    available = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch.object(sandbox, "_run_docker", return_value=available):
        sandbox.check_docker_available()


def test_docker_check_reports_unavailable_daemon():
    unavailable = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="EOF\n")
    with (
        patch.object(sandbox, "_run_docker", return_value=unavailable),
        pytest.raises(sandbox.SandboxUnavailable, match="Docker daemon is unavailable: EOF"),
    ):
        sandbox.check_docker_available()


def test_docker_check_treats_successful_exit_with_eof_as_unavailable():
    misleading = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr="Error reading remote info: EOF\n"
    )
    with (
        patch.object(sandbox, "_run_docker", return_value=misleading),
        pytest.raises(sandbox.SandboxUnavailable, match="Error reading remote info: EOF"),
    ):
        sandbox.check_docker_available()


def test_docker_check_reports_missing_docker_cli():
    with (
        patch.object(sandbox, "_run_docker", side_effect=FileNotFoundError),
        pytest.raises(sandbox.SandboxUnavailable, match="Docker CLI was not found"),
    ):
        sandbox.check_docker_available()


# --- build_image ----------------------------------------------------------------


def test_build_image_loads_locally_runnable_image():
    available = subprocess.CompletedProcess(args=[], returncode=0, stdout="29.0", stderr="")
    missing = subprocess.CompletedProcess(args=[], returncode=1)
    built = subprocess.CompletedProcess(args=[], returncode=0)
    with patch.object(sandbox, "_run_docker", side_effect=[available, missing, built]) as run:
        sandbox.build_image("test-image")

    assert "--load" in run.call_args_list[2].args[0]


def test_build_image_reports_timeout():
    available = subprocess.CompletedProcess(args=[], returncode=0, stdout="29.0", stderr="")
    missing = subprocess.CompletedProcess(args=[], returncode=1)
    with (
        patch.object(
            sandbox,
            "_run_docker",
            side_effect=[available, missing, subprocess.TimeoutExpired("docker", 7)],
        ),
        pytest.raises(sandbox.SandboxError, match="build exceeded the 7s timeout"),
    ):
        sandbox.build_image("test-image", build_timeout=7)


# --- _derived_image_tag / derive_image ------------------------------------------


def test_derived_image_tag_is_stable_and_content_addressed():
    text = "FROM base\nRUN pip install a b\n"
    tag1 = sandbox._derived_image_tag("pfind-search-paths:latest", text)
    tag2 = sandbox._derived_image_tag("pfind-search-paths:latest", text)
    assert tag1 == tag2
    assert tag1.startswith("pfind-search-paths:deps-")
    # Different Dockerfile text yields a different tag.
    assert tag1 != sandbox._derived_image_tag("pfind-search-paths:latest", text + "x")


def test_derive_image_builds_and_returns_tag():
    built = subprocess.CompletedProcess(args=[], returncode=0)
    box = DockerSandbox("base:latest", dockerfile="Dockerfile.python")
    with (
        patch.object(sandbox, "_image_exists", return_value=False),
        patch.object(sandbox, "_run_docker", return_value=built) as run,
    ):
        tag = box.derive_image("FROM base:latest\nRUN pip install mutagen\n")

    assert tag.startswith("base:deps-")
    assert run.call_args.args[0][0:2] == ["docker", "build"]


def test_derive_image_reuses_existing_image():
    box = DockerSandbox("base:latest", dockerfile="Dockerfile.python")
    with (
        patch.object(sandbox, "_image_exists", return_value=True),
        patch.object(sandbox, "_run_docker") as run,
    ):
        tag = box.derive_image("FROM base:latest\n")

    run.assert_not_called()
    assert tag.startswith("base:deps-")


# --- DockerSandbox.run: hardened flags and limit mapping ------------------------


def test_run_uses_security_and_resource_flags(tmp_path):
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=b'{"ok":true,"results":[]}', stderr=b""
    )
    box = DockerSandbox("img:latest", dockerfile="Dockerfile.python")
    with patch.object(sandbox, "_run_docker", return_value=completed) as run:
        result = box.run(
            b"{}",
            mounts=[Mount(tmp_path.resolve(), "/data", read_only=True)],
            limits=Limits(),
        )

    assert result.returncode == 0
    command = run.call_args.args[0]
    assert command[0:2] == ["docker", "run"]
    network_index = command.index("--network")
    assert command[network_index : network_index + 2] == ["--network", "none"]
    assert "--read-only" in command
    assert command[command.index("--cap-drop") : command.index("--cap-drop") + 2] == [
        "--cap-drop",
        "ALL",
    ]
    assert "no-new-privileges" in command
    assert f"type=bind,src={tmp_path.resolve()},dst=/data,readonly" in command
    assert command[-1] == "img:latest"


def test_run_maps_timeout_and_removes_container():
    box = DockerSandbox("img:latest", dockerfile="Dockerfile.python")
    with (
        patch.object(sandbox, "_run_docker", side_effect=subprocess.TimeoutExpired("docker", 10)),
        patch.object(sandbox, "_remove_container") as remove,
        pytest.raises(sandbox.SandboxTimeout, match="exceeded"),
    ):
        box.run(b"{}", mounts=[], limits=Limits(timeout=10))

    remove.assert_called_once()


def test_run_rejects_oversized_output():
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"x" * 10, stderr=b"")
    box = DockerSandbox("img:latest", dockerfile="Dockerfile.python")
    with (
        patch.object(sandbox, "_run_docker", return_value=completed),
        pytest.raises(sandbox.SandboxOutputTooLarge),
    ):
        box.run(b"{}", mounts=[], limits=Limits(max_output_bytes=4))


def test_run_docker_timeout_kills_plugin_process_group():
    process = Mock(pid=123, returncode=-9)
    process.communicate.side_effect = [
        subprocess.TimeoutExpired(["docker", "info"], 1),
        ("", ""),
    ]
    with (
        patch.object(sandbox.subprocess, "Popen", return_value=process),
        patch.object(sandbox.os, "name", "posix"),
        patch.object(sandbox.os, "killpg") as killpg,
        pytest.raises(subprocess.TimeoutExpired),
    ):
        sandbox._run_docker(["docker", "info"], timeout=1, capture_output=True)

    killpg.assert_called_once_with(123, sandbox.signal.SIGKILL)
