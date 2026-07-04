"""Pure container-mechanics tests targeting the sandbox backends.

These exercise the hardened flag sets, image build/derive, and the timeout/output-size
mapping directly against :mod:`nfind.sandbox`, patching the shared runner
``nfind.sandbox.base._run_cli`` (and the per-backend helpers) so they need no running
container runtime.
"""

import subprocess
import sys
from unittest.mock import Mock, patch

import pytest

from nfind import sandbox
from nfind.sandbox import (
    AppleContainerSandbox,
    DockerSandbox,
    Limits,
    Mount,
    PodmanSandbox,
    apple,
    base,
    docker,
    podman,
)


def test_docker_error_aliases_map_to_sandbox_hierarchy():
    # Existing `except DockerUnavailableError` / `except DockerError` call sites must keep
    # catching what the sandbox raises, so the aliases are the same objects.
    from nfind.errors import DockerError, DockerUnavailableError

    assert DockerUnavailableError is sandbox.SandboxUnavailable
    assert DockerError is sandbox.SandboxError
    assert issubclass(sandbox.SandboxUnavailable, DockerError)


def test_default_sandbox_backend_is_docker():
    assert sandbox.DEFAULT_SANDBOX_BACKEND == "docker"


# --- check_docker_available -----------------------------------------------------


def test_docker_check_accepts_empty_container_list():
    available = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch.object(base, "_run_cli", return_value=available):
        sandbox.check_docker_available()


def test_docker_check_reports_unavailable_daemon():
    unavailable = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="EOF\n")
    with (
        patch.object(base, "_run_cli", return_value=unavailable),
        pytest.raises(sandbox.SandboxUnavailable, match="Docker daemon is unavailable: EOF"),
    ):
        sandbox.check_docker_available()


def test_docker_check_treats_successful_exit_with_eof_as_unavailable():
    misleading = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr="Error reading remote info: EOF\n"
    )
    with (
        patch.object(base, "_run_cli", return_value=misleading),
        pytest.raises(sandbox.SandboxUnavailable, match="Error reading remote info: EOF"),
    ):
        sandbox.check_docker_available()


def test_docker_check_reports_missing_docker_cli():
    with (
        patch.object(base, "_run_cli", side_effect=FileNotFoundError),
        pytest.raises(sandbox.SandboxUnavailable, match="Docker CLI was not found"),
    ):
        sandbox.check_docker_available()


# --- build_image ----------------------------------------------------------------


def test_build_image_loads_locally_runnable_image():
    available = subprocess.CompletedProcess(args=[], returncode=0, stdout="29.0", stderr="")
    missing = subprocess.CompletedProcess(args=[], returncode=1)
    built = subprocess.CompletedProcess(args=[], returncode=0)
    with (
        patch.object(base, "_run_cli", side_effect=[available, missing, built]) as run,
        patch.object(docker, "_docker_build_supports_load", return_value=True),
    ):
        sandbox.build_image("test-image")

    assert "--load" in run.call_args_list[2].args[0]


def test_build_image_omits_load_when_docker_build_rejects_it():
    available = subprocess.CompletedProcess(args=[], returncode=0, stdout="29.0", stderr="")
    missing = subprocess.CompletedProcess(args=[], returncode=1)
    built = subprocess.CompletedProcess(args=[], returncode=0)
    with (
        patch.object(base, "_run_cli", side_effect=[available, missing, built]) as run,
        patch.object(docker, "_docker_build_supports_load", return_value=False),
    ):
        sandbox.build_image("test-image")

    assert "--load" not in run.call_args_list[2].args[0]


def test_build_image_reports_timeout():
    available = subprocess.CompletedProcess(args=[], returncode=0, stdout="29.0", stderr="")
    missing = subprocess.CompletedProcess(args=[], returncode=1)
    with (
        patch.object(
            base,
            "_run_cli",
            side_effect=[available, missing, subprocess.TimeoutExpired("docker", 7)],
        ),
        patch.object(docker, "_docker_build_supports_load", return_value=False),
        pytest.raises(sandbox.SandboxError, match="build exceeded the 7s timeout"),
    ):
        sandbox.build_image("test-image", build_timeout=7)


# --- derived_image_tag / derive_image ------------------------------------------


def test_derived_image_tag_is_stable_and_content_addressed():
    text = "FROM base\nRUN pip install a b\n"
    tag1 = sandbox.derived_image_tag("nfind-search-paths:latest", text)
    tag2 = sandbox.derived_image_tag("nfind-search-paths:latest", text)
    assert tag1 == tag2
    assert tag1.startswith("nfind-search-paths:deps-")
    # Different Dockerfile text yields a different tag.
    assert tag1 != sandbox.derived_image_tag("nfind-search-paths:latest", text + "x")


def test_derive_image_builds_and_returns_tag():
    built = subprocess.CompletedProcess(args=[], returncode=0)
    box = DockerSandbox("base:latest", dockerfile="Dockerfile.python")
    with (
        patch.object(docker, "_image_exists", return_value=False),
        patch.object(base, "_run_cli", return_value=built) as run,
        patch.object(docker, "_docker_build_supports_load", return_value=True),
    ):
        tag = box.derive_image("FROM base:latest\nRUN pip install mutagen\n")

    assert tag.startswith("base:deps-")
    assert run.call_args.args[0][0:2] == ["docker", "build"]
    assert "--load" in run.call_args.args[0]


def test_derive_image_omits_load_when_docker_build_rejects_it():
    built = subprocess.CompletedProcess(args=[], returncode=0)
    box = DockerSandbox("base:latest", dockerfile="Dockerfile.python")
    with (
        patch.object(docker, "_image_exists", return_value=False),
        patch.object(base, "_run_cli", return_value=built) as run,
        patch.object(docker, "_docker_build_supports_load", return_value=False),
    ):
        tag = box.derive_image("FROM base:latest\nRUN pip install mutagen\n")

    assert tag.startswith("base:deps-")
    assert run.call_args.args[0][0:2] == ["docker", "build"]
    assert "--load" not in run.call_args.args[0]


def test_ensure_image_delegates_to_build_image():
    box = DockerSandbox("img:latest", dockerfile="Dockerfile.python", build_timeout=42)
    with patch.object(docker, "build_image") as build:
        box.ensure_image(rebuild=True)
    build.assert_called_once_with(
        "img:latest", rebuild=True, build_timeout=42, dockerfile="Dockerfile.python"
    )


def test_derive_image_reuses_existing_image():
    box = DockerSandbox("base:latest", dockerfile="Dockerfile.python")
    with (
        patch.object(docker, "_image_exists", return_value=True),
        patch.object(base, "_run_cli") as run,
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
    with patch.object(base, "_run_cli", return_value=completed) as run:
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
        patch.object(base, "_run_cli", side_effect=subprocess.TimeoutExpired("docker", 10)),
        patch.object(docker, "_remove_container") as remove,
        pytest.raises(sandbox.SandboxTimeout, match="exceeded"),
    ):
        box.run(b"{}", mounts=[], limits=Limits(timeout=10))

    remove.assert_called_once()


def test_run_removes_container_on_whole_command_interrupt():
    box = DockerSandbox("img:latest", dockerfile="Dockerfile.python")
    with (
        patch.object(base, "_run_cli", side_effect=KeyboardInterrupt),
        patch.object(docker, "_remove_container") as remove,
        pytest.raises(KeyboardInterrupt),
    ):
        box.run(b"{}", mounts=[], limits=Limits())

    remove.assert_called_once()


def test_run_removes_container_on_command_timeout():
    # The whole-command deadline raises CommandTimeoutError mid-run; unlike the interrupt
    # case it derives from TimeoutError, so pin that the same cleanup path fires and the
    # concrete exception (not a SandboxTimeout) still propagates unchanged.
    from nfind.deadline import CommandTimeoutError

    box = DockerSandbox("img:latest", dockerfile="Dockerfile.python")
    with (
        patch.object(base, "_run_cli", side_effect=CommandTimeoutError("deadline")),
        patch.object(docker, "_remove_container") as remove,
        pytest.raises(CommandTimeoutError),
    ):
        box.run(b"{}", mounts=[], limits=Limits())

    remove.assert_called_once()


def test_run_rejects_oversized_output():
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"x" * 10, stderr=b"")
    box = DockerSandbox("img:latest", dockerfile="Dockerfile.python")
    with (
        patch.object(base, "_run_cli", return_value=completed),
        pytest.raises(sandbox.SandboxOutputTooLarge),
    ):
        box.run(b"{}", mounts=[], limits=Limits(max_output_bytes=4))


# --- AppleContainerSandbox ------------------------------------------------------


def test_check_apple_container_available_uses_system_status():
    available = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
    with patch.object(base, "_run_cli", return_value=available) as run:
        sandbox.check_apple_container_available()

    assert run.call_args.args[0] == ["container", "system", "status", "--format", "json"]


def test_check_apple_container_available_reports_missing_cli():
    with (
        patch.object(base, "_run_cli", side_effect=FileNotFoundError),
        pytest.raises(sandbox.SandboxUnavailable, match="Apple container CLI was not found"),
    ):
        sandbox.check_apple_container_available()


def test_build_apple_container_image_omits_docker_load_flag():
    available = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
    missing = subprocess.CompletedProcess(args=[], returncode=1)
    built = subprocess.CompletedProcess(args=[], returncode=0)
    with patch.object(
        base,
        "_run_cli",
        side_effect=[available, missing, built],
    ) as run:
        sandbox.build_apple_container_image("test-image")

    command = run.call_args_list[2].args[0]
    assert command[0:2] == ["container", "build"]
    assert "--load" not in command
    assert "--tag" in command and "test-image" in command


def test_apple_derive_image_builds_and_returns_tag():
    built = subprocess.CompletedProcess(args=[], returncode=0)
    box = AppleContainerSandbox("base:latest", dockerfile="Dockerfile.python")
    with (
        patch.object(apple, "_apple_image_exists", return_value=False),
        patch.object(base, "_run_cli", return_value=built) as run,
    ):
        tag = box.derive_image("FROM base:latest\nRUN pip install mutagen\n")

    assert tag.startswith("base:deps-")
    assert run.call_args.args[0][0:2] == ["container", "build"]
    assert "--load" not in run.call_args.args[0]


def test_apple_run_uses_supported_security_and_resource_flags(tmp_path):
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=b'{"ok":true,"results":[]}', stderr=b""
    )
    box = AppleContainerSandbox("img:latest", dockerfile="Dockerfile.python")
    with patch.object(base, "_run_cli", return_value=completed) as run:
        result = box.run(
            b"{}",
            mounts=[Mount(tmp_path.resolve(), "/data", read_only=True)],
            limits=Limits(),
        )

    assert result.returncode == 0
    command = run.call_args.args[0]
    assert command[0:2] == ["container", "run"]
    assert "--read-only" in command
    assert command[command.index("--cap-drop") : command.index("--cap-drop") + 2] == [
        "--cap-drop",
        "ALL",
    ]
    assert "--no-dns" in command
    assert "--network" not in command
    assert "--pids-limit" not in command
    assert "--security-opt" not in command
    assert command[command.index("--cpus") : command.index("--cpus") + 2] == ["--cpus", "1"]
    assert f"type=bind,source={tmp_path.resolve()},target=/data,readonly" in command
    assert command[-1] == "img:latest"


def test_apple_run_uses_no_network_on_macos_26(tmp_path):
    box = AppleContainerSandbox("img:latest", dockerfile="Dockerfile.python")

    with patch.object(apple.platform, "mac_ver", return_value=("26.0", ("", "", ""), "")):
        command = box._build_run_command(
            "name",
            [Mount(tmp_path.resolve(), "/data", read_only=True)],
            Limits(),
        )

    assert command[command.index("--network") : command.index("--network") + 2] == [
        "--network",
        "none",
    ]
    assert "--no-dns" not in command


def test_apple_run_falls_back_to_no_dns_before_macos_26(tmp_path):
    box = AppleContainerSandbox("img:latest", dockerfile="Dockerfile.python")

    with patch.object(apple.platform, "mac_ver", return_value=("15.7.3", ("", "", ""), "")):
        command = box._build_run_command(
            "name",
            [Mount(tmp_path.resolve(), "/data", read_only=True)],
            Limits(),
        )

    assert "--no-dns" in command
    assert "--network" not in command


def test_apple_run_rejects_fractional_cpus_before_running(tmp_path):
    box = AppleContainerSandbox("img:latest", dockerfile="Dockerfile.python")
    with (
        patch.object(base, "_run_cli") as run,
        pytest.raises(ValueError, match="requires --cpus to be a whole number"),
    ):
        box.run(
            b"{}",
            mounts=[Mount(tmp_path.resolve(), "/data", read_only=True)],
            limits=Limits(cpus=0.5),
        )

    run.assert_not_called()


def test_apple_run_maps_timeout_and_removes_container():
    box = AppleContainerSandbox("img:latest", dockerfile="Dockerfile.python")
    with (
        patch.object(base, "_run_cli", side_effect=subprocess.TimeoutExpired("container", 10)),
        patch.object(apple, "_remove_apple_container") as remove,
        pytest.raises(sandbox.SandboxTimeout, match="exceeded"),
    ):
        box.run(b"{}", mounts=[], limits=Limits(timeout=10))

    remove.assert_called_once()


def test_create_sandbox_returns_requested_backend():
    docker_box = sandbox.create_sandbox("docker", "img:latest", dockerfile="Dockerfile.python")
    apple_box = sandbox.create_sandbox("apple", "img:latest", dockerfile="Dockerfile.python")
    podman_box = sandbox.create_sandbox("podman", "img:latest", dockerfile="Dockerfile.python")

    assert isinstance(docker_box, DockerSandbox)
    assert isinstance(apple_box, AppleContainerSandbox)
    assert isinstance(podman_box, PodmanSandbox)


# --- PodmanSandbox --------------------------------------------------------------


def test_check_podman_available_probes_with_podman_ps():
    available = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch.object(base, "_run_cli", return_value=available) as run:
        sandbox.check_podman_available()

    assert run.call_args.args[0] == ["podman", "ps", "--quiet", "--no-trunc"]


def test_check_podman_available_reports_missing_cli():
    with (
        patch.object(base, "_run_cli", side_effect=FileNotFoundError),
        pytest.raises(sandbox.SandboxUnavailable, match="Podman CLI was not found"),
    ):
        sandbox.check_podman_available()


def test_check_podman_available_reports_unavailable_machine():
    unavailable = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="no machine\n"
    )
    with (
        patch.object(base, "_run_cli", return_value=unavailable),
        pytest.raises(sandbox.SandboxUnavailable, match="Podman is unavailable: no machine"),
    ):
        sandbox.check_podman_available()


def test_build_podman_image_omits_docker_load_flag():
    available = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    missing = subprocess.CompletedProcess(args=[], returncode=1)
    built = subprocess.CompletedProcess(args=[], returncode=0)
    with patch.object(base, "_run_cli", side_effect=[available, missing, built]) as run:
        sandbox.build_podman_image("test-image")

    command = run.call_args_list[2].args[0]
    assert command[0:2] == ["podman", "build"]
    assert "--load" not in command
    assert "--tag" in command and "test-image" in command


def test_podman_derive_image_builds_and_returns_tag():
    built = subprocess.CompletedProcess(args=[], returncode=0)
    box = PodmanSandbox("base:latest", dockerfile="Dockerfile.python")
    with (
        patch.object(podman, "_podman_image_exists", return_value=False),
        patch.object(base, "_run_cli", return_value=built) as run,
    ):
        tag = box.derive_image("FROM base:latest\nRUN pip install mutagen\n")

    assert tag.startswith("base:deps-")
    assert run.call_args.args[0][0:2] == ["podman", "build"]
    assert "--load" not in run.call_args.args[0]


def test_podman_derive_image_reports_build_failure():
    box = PodmanSandbox("base:latest", dockerfile="Dockerfile.python")
    with (
        patch.object(podman, "_podman_image_exists", return_value=False),
        patch.object(base, "_run_cli", return_value=_cp(1)),
        pytest.raises(sandbox.SandboxError, match="Failed to build the derived Podman image"),
    ):
        box.derive_image("FROM base:latest\n")


def test_podman_run_uses_docker_family_security_flags(tmp_path):
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=b'{"ok":true,"results":[]}', stderr=b""
    )
    box = PodmanSandbox("img:latest", dockerfile="Dockerfile.python")
    with patch.object(base, "_run_cli", return_value=completed) as run:
        result = box.run(
            b"{}",
            mounts=[Mount(tmp_path.resolve(), "/data", read_only=True)],
            limits=Limits(),
        )

    assert result.returncode == 0
    command = run.call_args.args[0]
    assert command[0:2] == ["podman", "run"]
    network_index = command.index("--network")
    assert command[network_index : network_index + 2] == ["--network", "none"]
    assert "--read-only" in command
    assert "no-new-privileges" in command
    assert "--pids-limit" in command
    assert f"type=bind,src={tmp_path.resolve()},dst=/data,readonly" in command
    assert command[-1] == "img:latest"


def test_podman_and_docker_share_identical_hardening(tmp_path):
    # The Docker-family run command must differ only by the executable name, so the two
    # backends cannot drift on the security-critical flag set.
    mounts = [Mount(tmp_path.resolve(), "/data", read_only=True)]
    limits = Limits()
    docker_cmd = DockerSandbox("img:latest")._build_run_command("box", mounts, limits)
    podman_cmd = PodmanSandbox("img:latest")._build_run_command("box", mounts, limits)

    assert docker_cmd[0] == "docker" and podman_cmd[0] == "podman"
    assert docker_cmd[1:] == podman_cmd[1:]


def test_podman_run_maps_timeout_and_removes_container():
    box = PodmanSandbox("img:latest", dockerfile="Dockerfile.python")
    with (
        patch.object(base, "_run_cli", side_effect=subprocess.TimeoutExpired("podman", 10)),
        patch.object(podman, "_remove_podman_container") as remove,
        pytest.raises(sandbox.SandboxTimeout, match="exceeded"),
    ):
        box.run(b"{}", mounts=[], limits=Limits(timeout=10))

    remove.assert_called_once()


def test_ensure_podman_image_delegates_to_build():
    box = PodmanSandbox("img:latest", dockerfile="Dockerfile.python", build_timeout=42)
    with patch.object(podman, "build_podman_image") as build:
        box.ensure_image(rebuild=True)
    build.assert_called_once_with(
        "img:latest", rebuild=True, build_timeout=42, dockerfile="Dockerfile.python"
    )


def test_remove_podman_container_invokes_podman_rm():
    with patch.object(base, "_run_cli") as run:
        podman._remove_podman_container("c1")
    assert run.call_args.args[0] == ["podman", "rm", "--force", "c1"]


@pytest.mark.skipif(sys.platform == "win32", reason="os.killpg and signal.SIGKILL are POSIX-only")
def test_run_cli_timeout_kills_plugin_process_group():
    process = Mock(pid=123, returncode=-9)
    process.communicate.side_effect = [
        subprocess.TimeoutExpired(["docker", "info"], 1),
        ("", ""),
    ]
    with (
        patch.object(base.subprocess, "Popen", return_value=process),
        patch.object(base.os, "name", "posix"),
        patch.object(base.os, "killpg", create=True) as killpg,
        pytest.raises(subprocess.TimeoutExpired),
    ):
        base._run_cli(["docker", "info"], timeout=1, capture_output=True)

    killpg.assert_called_once_with(123, base.signal.SIGKILL)


def test_run_cli_interrupt_kills_plugin_process_group():
    process = Mock(pid=123, returncode=None)
    process.communicate.side_effect = KeyboardInterrupt
    with (
        patch.object(base.subprocess, "Popen", return_value=process),
        patch.object(base.os, "name", "posix"),
        patch.object(base.os, "killpg", create=True) as killpg,
        pytest.raises(KeyboardInterrupt),
    ):
        base._run_cli(["docker", "info"], timeout=10, capture_output=True)

    killpg.assert_called_once_with(123, base.signal.SIGKILL)


# --- _run_cli: capture-output plumbing ------------------------------------------


def _cp(returncode: int = 0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_cli_rejects_stdout_with_capture_output():
    with pytest.raises(ValueError, match="cannot be used with capture_output"):
        base._run_cli(["docker", "ps"], timeout=1, capture_output=True, stdout=subprocess.DEVNULL)


def test_run_cli_reads_captured_output_as_text():
    process = Mock(returncode=0)
    process.communicate.return_value = (None, None)
    with patch.object(base.subprocess, "Popen", return_value=process):
        result = base._run_cli(["docker", "ps"], timeout=1, capture_output=True, text=True)
    # The (mocked) container writes nothing, so the captured temp files decode to "".
    assert result.returncode == 0
    assert result.stdout == "" and result.stderr == ""


def test_run_cli_timeout_on_non_posix_reads_captured_text():
    process = Mock(pid=1, returncode=None)
    process.communicate.side_effect = subprocess.TimeoutExpired(["docker", "ps"], 1)
    process.poll.return_value = None  # still alive after the grace wait -> hard kill
    with (
        patch.object(base.subprocess, "Popen", return_value=process),
        patch.object(base.os, "name", "nt"),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        base._run_cli(["docker", "ps"], timeout=1, capture_output=True, text=True)
    assert process.kill.call_count >= 1


# --- check_docker_available / build_image: remaining branches -------------------


def test_docker_check_reports_daemon_timeout():
    with (
        patch.object(base, "_run_cli", side_effect=subprocess.TimeoutExpired("docker", 10)),
        pytest.raises(sandbox.SandboxUnavailable, match="did not respond"),
    ):
        sandbox.check_docker_available()


def test_build_image_rejects_nonpositive_timeout():
    with pytest.raises(ValueError, match="build_timeout must be positive"):
        sandbox.build_image("img", build_timeout=0)


def test_build_image_skips_build_when_image_present():
    with patch.object(base, "_run_cli", side_effect=[_cp(0), _cp(0)]) as run:
        sandbox.build_image("img")
    # docker ps (availability) + docker image inspect (found) -> no build.
    assert run.call_count == 2


def test_build_image_reports_inspect_timeout():
    with (
        patch.object(
            base, "_run_cli", side_effect=[_cp(0), subprocess.TimeoutExpired("docker", 5)]
        ),
        pytest.raises(sandbox.SandboxUnavailable, match="inspecting"),
    ):
        sandbox.build_image("img")


def test_build_image_reports_build_failure():
    with (
        patch.object(base, "_run_cli", side_effect=[_cp(0), _cp(1), _cp(1)]),
        patch.object(docker, "_docker_build_supports_load", return_value=False),
        pytest.raises(sandbox.SandboxError, match="build failed with exit status 1"),
    ):
        sandbox.build_image("img")


def test_docker_supports_linux_containers_true_for_linux_ostype():
    with patch.object(base, "_run_cli", return_value=_cp(0, stdout="linux\n")):
        assert sandbox.docker_supports_linux_containers() is True


def test_docker_supports_linux_containers_false_for_windows_ostype():
    with patch.object(base, "_run_cli", return_value=_cp(0, stdout="windows\n")):
        assert sandbox.docker_supports_linux_containers() is False


# --- _image_exists / _remove_container / derive_image ---------------------------


def test_image_exists_true_when_inspect_succeeds():
    with patch.object(base, "_run_cli", return_value=_cp(0)):
        assert docker._image_exists("img") is True


def test_image_exists_false_when_inspect_fails():
    with patch.object(base, "_run_cli", return_value=_cp(1)):
        assert docker._image_exists("img") is False


def test_image_exists_reports_timeout():
    with (
        patch.object(base, "_run_cli", side_effect=subprocess.TimeoutExpired("docker", 10)),
        pytest.raises(sandbox.SandboxUnavailable, match="inspecting"),
    ):
        docker._image_exists("img")


def test_remove_container_invokes_docker_rm():
    with patch.object(base, "_run_cli") as run:
        docker._remove_container("c1")
    assert run.call_args.args[0] == ["docker", "rm", "--force", "c1"]


def test_remove_container_suppresses_errors():
    with patch.object(base, "_run_cli", side_effect=FileNotFoundError):
        docker._remove_container("c1")  # must not raise


def test_derive_image_reports_build_failure():
    box = DockerSandbox("base:latest", dockerfile="Dockerfile.python")
    with (
        patch.object(docker, "_image_exists", return_value=False),
        patch.object(base, "_run_cli", return_value=_cp(1)),
        pytest.raises(sandbox.SandboxError, match="Failed to build the derived"),
    ):
        box.derive_image("FROM base:latest\n")


def test_derive_image_reports_build_timeout():
    box = DockerSandbox("base:latest", dockerfile="Dockerfile.python", build_timeout=5)
    with (
        patch.object(docker, "_image_exists", return_value=False),
        patch.object(base, "_run_cli", side_effect=subprocess.TimeoutExpired("docker", 5)),
        pytest.raises(sandbox.SandboxError, match="exceeded the 5s timeout"),
    ):
        box.derive_image("FROM base:latest\n")
