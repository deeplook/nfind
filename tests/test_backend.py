import json
from unittest.mock import Mock, patch

import pytest

from pfind import backend as MODULE
from pfind import cli


def _gen(code, dependencies=()):
    return MODULE.GeneratedFilter(code=code, dependencies=list(dependencies))


def _gen_node(code, dependencies=()):
    return MODULE.GeneratedFilter(code=code, dependencies=list(dependencies), runtime="node")


def test_validate_code_shape_accepts_one_filter_function():
    MODULE._validate_code_shape("def filter_paths(paths):\n    return paths")


@pytest.mark.parametrize(
    "code",
    [
        "print('top level')\ndef filter_paths(paths): return paths",
        "def other(paths): return paths",
        "@print('decorator')\ndef filter_paths(paths): return paths",
        "def filter_paths(paths, extra): return paths",
    ],
)
def test_validate_code_shape_rejects_invalid_interface(code):
    with pytest.raises(ValueError):
        MODULE._validate_code_shape(code)


def test_enumerate_paths_maps_container_paths_to_host(tmp_path):
    (tmp_path / "folder").mkdir()
    (tmp_path / "folder" / "example.txt").write_text("example")

    paths, mapping = MODULE.enumerate_paths(tmp_path)

    assert "/data/folder" in paths
    assert "/data/folder/example.txt" in paths
    assert mapping["/data/folder/example.txt"] == str(tmp_path / "folder" / "example.txt")


def test_worker_rejects_results_not_supplied_by_host():
    payload = {
        "code": "def filter_paths(paths):\n    return ['/data/not-supplied']",
        "paths": ["/data/supplied"],
    }

    with pytest.raises(ValueError, match="outside its input set"):
        MODULE._worker_response(payload)


def test_worker_filter_can_read_file_content(tmp_path):
    path = tmp_path / "example.txt"
    path.write_text("needle")
    payload = {
        "code": (
            "def filter_paths(paths):\n"
            "    return [path for path in paths if open(path).read() == 'needle']"
        ),
        "paths": [str(path)],
    }

    assert MODULE._worker_response(payload) == {"ok": True, "results": [{"path": str(path)}]}


def test_build_image_loads_locally_runnable_image():
    available = MODULE.subprocess.CompletedProcess(args=[], returncode=0, stdout="29.0", stderr="")
    missing = MODULE.subprocess.CompletedProcess(args=[], returncode=1)
    built = MODULE.subprocess.CompletedProcess(args=[], returncode=0)
    with patch.object(MODULE, "_run_docker", side_effect=[available, missing, built]) as run:
        MODULE.build_image("test-image")

    assert "--load" in run.call_args_list[2].args[0]


def test_build_image_reports_unavailable_daemon():
    unavailable = MODULE.subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="EOF\n"
    )
    with (
        patch.object(MODULE, "_run_docker", return_value=unavailable),
        pytest.raises(MODULE.DockerUnavailableError, match="Docker daemon is unavailable: EOF"),
    ):
        MODULE.build_image("test-image")


def test_docker_check_accepts_empty_container_list():
    available = MODULE.subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch.object(MODULE, "_run_docker", return_value=available):
        MODULE.check_docker_available()


def test_build_image_treats_successful_exit_with_eof_as_unavailable():
    misleading = MODULE.subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr="Error reading remote info: EOF\n"
    )
    with (
        patch.object(MODULE, "_run_docker", return_value=misleading),
        pytest.raises(MODULE.DockerUnavailableError, match="Error reading remote info: EOF"),
    ):
        MODULE.build_image("test-image")


def test_build_image_reports_missing_docker_cli():
    with (
        patch.object(MODULE, "_run_docker", side_effect=FileNotFoundError),
        pytest.raises(MODULE.DockerUnavailableError, match="Docker CLI was not found"),
    ):
        MODULE.build_image("test-image")


def test_build_image_reports_timeout():
    available = MODULE.subprocess.CompletedProcess(args=[], returncode=0, stdout="29.0", stderr="")
    missing = MODULE.subprocess.CompletedProcess(args=[], returncode=1)
    with (
        patch.object(
            MODULE,
            "_run_docker",
            side_effect=[available, missing, MODULE.subprocess.TimeoutExpired("docker", 7)],
        ),
        pytest.raises(MODULE.DockerError, match="build exceeded the 7s timeout"),
    ):
        MODULE.build_image("test-image", build_timeout=7)


def test_search_checks_docker_before_generating_code(tmp_path):
    (tmp_path / "file.txt").write_text("content")
    with (
        patch.object(
            MODULE, "check_docker_available", side_effect=MODULE.DockerUnavailableError("offline")
        ),
        patch.object(MODULE, "generate_filter") as generate,
        pytest.raises(MODULE.DockerUnavailableError),
    ):
        MODULE.search(str(tmp_path), "files")

    generate.assert_not_called()


def test_normalize_results_accepts_paths_and_records():
    allowed = {"/data/a", "/data/b"}
    assert MODULE._normalize_results(["/data/a"], allowed) == [{"path": "/data/a"}]
    assert MODULE._normalize_results([{"path": "/data/b", "lines": 3}], allowed) == [
        {"path": "/data/b", "lines": 3}
    ]


@pytest.mark.parametrize(
    "results",
    [
        ["/data/missing"],
        [{"path": "/data/missing"}],
        [{"lines": 3}],
        ["ok", 5],
        "notalist",
    ],
)
def test_normalize_results_rejects_bad_output(results):
    with pytest.raises(ValueError):
        MODULE._normalize_results(results, {"/data/a"})


def test_worker_returns_extra_fields(tmp_path):
    path = tmp_path / "example.txt"
    path.write_text("a\nb\nc\n")
    payload = {
        "code": (
            "def filter_paths(paths):\n"
            "    return [{'path': p, 'lines': len(open(p).read().splitlines())} for p in paths]"
        ),
        "paths": [str(path)],
    }

    assert MODULE._worker_response(payload) == {
        "ok": True,
        "results": [{"path": str(path), "lines": 3}],
    }


def test_search_maps_host_paths_in_records(tmp_path):
    (tmp_path / "file.txt").write_text("content")
    with (
        patch.object(MODULE, "check_docker_available"),
        patch.object(MODULE, "build_worker_image", return_value=MODULE.DEFAULT_IMAGE),
        patch.object(
            MODULE, "generate_filter", return_value=_gen("def filter_paths(paths): return paths")
        ),
        patch.object(
            MODULE,
            "run_filter",
            return_value=[{"path": "/data/file.txt", "lines": 1}],
        ),
    ):
        results = MODULE.search(str(tmp_path), "files")

    assert results == [{"path": str((tmp_path / "file.txt").resolve()), "lines": 1}]


def test_search_invokes_on_generated_before_running(tmp_path):
    (tmp_path / "file.txt").write_text("content")
    seen: list[str] = []
    with (
        patch.object(MODULE, "check_docker_available"),
        patch.object(MODULE, "build_worker_image", return_value=MODULE.DEFAULT_IMAGE),
        patch.object(
            MODULE, "generate_filter", return_value=_gen("def filter_paths(paths): return []")
        ),
        patch.object(MODULE, "run_filter", return_value=[]) as run_filter,
    ):
        MODULE.search(str(tmp_path), "files", on_generated=seen.append)

    assert [g.code for g in seen] == ["def filter_paths(paths): return []"]
    run_filter.assert_called_once()


def test_search_aborts_when_on_generated_raises(tmp_path):
    (tmp_path / "file.txt").write_text("content")

    def decline(code: str) -> None:
        raise RuntimeError("declined")

    with (
        patch.object(MODULE, "check_docker_available"),
        patch.object(MODULE, "build_worker_image", return_value=MODULE.DEFAULT_IMAGE),
        patch.object(
            MODULE, "generate_filter", return_value=_gen("def filter_paths(paths): return []")
        ),
        patch.object(MODULE, "run_filter") as run_filter,
        pytest.raises(RuntimeError, match="declined"),
    ):
        MODULE.search(str(tmp_path), "files", on_generated=decline)

    run_filter.assert_not_called()


def test_search_rejects_unapproved_dependencies(tmp_path):
    (tmp_path / "file.txt").write_text("content")
    with (
        patch.object(MODULE, "check_docker_available"),
        patch.object(MODULE, "build_worker_image") as build,
        patch.object(
            MODULE,
            "generate_filter",
            return_value=_gen("def filter_paths(paths): return paths", ["sketchy-pkg"]),
        ),
        patch.object(MODULE, "run_filter") as run_filter,
        pytest.raises(MODULE.DependencyError, match="sketchy-pkg"),
    ):
        MODULE.search(str(tmp_path), "files", whitelist=set())

    build.assert_not_called()
    run_filter.assert_not_called()


def test_search_uses_whitelisted_dependency_without_prompt(tmp_path):
    (tmp_path / "file.txt").write_text("content")
    with (
        patch.object(MODULE, "check_docker_available"),
        patch.object(MODULE, "build_worker_image", return_value="img:deps") as build,
        patch.object(
            MODULE,
            "generate_filter",
            return_value=_gen("def filter_paths(paths): return paths", ["mutagen"]),
        ),
        patch.object(MODULE, "run_filter", return_value=[]) as run_filter,
        patch.object(MODULE, "approve_packages") as persist,
    ):
        # "mutagen" is in the default whitelist, so no approver call is needed.
        MODULE.search(str(tmp_path), "files", approve_dependencies=lambda pkgs: False)

    persist.assert_not_called()
    assert build.call_args.args[1] == ["mutagen"]
    run_filter.assert_called_once()
    assert run_filter.call_args.kwargs["image"] == "img:deps"


def test_search_approves_new_dependency_and_persists(tmp_path):
    (tmp_path / "file.txt").write_text("content")
    asked: list[list[str]] = []
    with (
        patch.object(MODULE, "check_docker_available"),
        patch.object(MODULE, "build_worker_image", return_value="img:deps") as build,
        patch.object(
            MODULE,
            "generate_filter",
            return_value=_gen("def filter_paths(paths): return paths", ["rarfile"]),
        ),
        patch.object(MODULE, "run_filter", return_value=[]),
        patch.object(MODULE, "approve_packages") as persist,
    ):

        def approver(pkgs):
            asked.append(pkgs)
            return True

        MODULE.search(str(tmp_path), "files", whitelist=set(), approve_dependencies=approver)

    assert asked == [["rarfile"]]
    persist.assert_called_once_with(["rarfile"], "python")
    assert build.call_args.args[1] == ["rarfile"]


def test_cli_confirm_aborts_without_running(tmp_path):
    from typer.testing import CliRunner

    (tmp_path / "file.txt").write_text("content")
    runner = CliRunner()
    with (
        patch.object(cli.backend, "check_docker_available"),
        patch.object(cli.backend, "build_worker_image", return_value=cli.backend.DEFAULT_IMAGE),
        patch.object(
            cli.backend, "generate_filter", return_value=_gen("def filter_paths(paths): return []")
        ),
        patch.object(cli.backend, "run_filter") as run_filter,
    ):
        result = runner.invoke(cli.app, ["files", str(tmp_path), "--confirm"], input="n\n")

    assert result.exit_code != 0
    assert "def filter_paths" in result.output
    run_filter.assert_not_called()


def test_parse_generation_accepts_node_runtime():
    content = json.dumps(
        {
            "runtime": "node",
            "dependencies": ["ts-morph"],
            "code": "function filterPaths(paths){ return paths; }",
        }
    )
    result = MODULE._parse_generation(content)
    assert result.runtime == "node"
    assert result.dependencies == ["ts-morph"]


def test_parse_generation_defaults_runtime_to_python():
    content = json.dumps({"code": "def filter_paths(paths): return paths"})
    assert MODULE._parse_generation(content).runtime == "python"


def test_parse_generation_rejects_unknown_runtime():
    content = json.dumps({"runtime": "ruby", "code": "def filter_paths(paths): return paths"})
    with pytest.raises(ValueError, match="Unknown runtime"):
        MODULE._parse_generation(content)


def test_node_runtime_validates_code_and_scoped_packages():
    MODULE.NODE_RUNTIME.validate_code("const filterPaths = (paths) => paths;")
    assert MODULE.NODE_RUNTIME.validate_packages(["@babel/parser", "ts-morph"]) == [
        "@babel/parser",
        "ts-morph",
    ]
    with pytest.raises(ValueError):
        MODULE.NODE_RUNTIME.validate_code("const other = 1;")
    with pytest.raises(ValueError):
        MODULE.PYTHON_RUNTIME.validate_packages(["@babel/parser"])  # scoped invalid for pip


def test_search_uses_node_base_image_and_runtime(tmp_path):
    (tmp_path / "a.ts").write_text("export const x = 1;")
    captured = {}

    def fake_build(image, dependencies, *, runtime, rebuild, build_timeout):
        captured["image"] = image
        captured["runtime"] = runtime.name
        return "pfind-search-node:deps-x"

    with (
        patch.object(MODULE, "check_docker_available"),
        patch.object(
            MODULE,
            "generate_filter",
            return_value=_gen_node("function filterPaths(p){return p;}", ["ts-morph"]),
        ),
        patch.object(MODULE, "build_worker_image", side_effect=fake_build),
        patch.object(MODULE, "run_filter", return_value=[]) as run_filter,
    ):
        MODULE.search(str(tmp_path), "typescript files")

    assert captured == {"image": MODULE.DEFAULT_NODE_IMAGE, "runtime": "node"}
    assert run_filter.call_args.kwargs["image"] == "pfind-search-node:deps-x"


def test_derived_dockerfile_uses_pip_or_npm():
    py = MODULE.PYTHON_RUNTIME.derived_dockerfile("base", ["mutagen"])
    assert "pip install" in py and "USER worker" in py
    node = MODULE.NODE_RUNTIME.derived_dockerfile("base", ["ts-morph"])
    assert "npm install" in node and "USER node" in node


def test_whitelist_is_per_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("PFIND_WHITELIST", str(tmp_path / "wl.json"))
    MODULE.approve_packages(["rarfile"], "python")
    MODULE.approve_packages(["left-pad"], "node")
    assert "rarfile" in MODULE.load_whitelist("python")
    assert "rarfile" not in MODULE.load_whitelist("node")
    assert "left-pad" in MODULE.load_whitelist("node")
    assert "ts-morph" in MODULE.load_whitelist("node")  # node default


def test_parse_generation_extracts_code_and_dependencies():
    content = json.dumps(
        {"dependencies": ["Mutagen", "mutagen"], "code": "def filter_paths(paths): return paths"}
    )
    result = MODULE._parse_generation(content)
    assert result.code == "def filter_paths(paths): return paths"
    assert result.dependencies == ["mutagen"]


def test_parse_generation_defaults_dependencies_to_empty():
    content = json.dumps({"code": "def filter_paths(paths): return paths"})
    assert MODULE._parse_generation(content).dependencies == []


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"dependencies": []}),  # missing code
        json.dumps({"code": "def other(paths): return paths"}),  # wrong shape
        json.dumps({"code": "def filter_paths(paths): return paths", "dependencies": ["bad name"]}),
        json.dumps({"code": "def filter_paths(paths): return paths", "dependencies": "mutagen"}),
    ],
)
def test_parse_generation_rejects_invalid(content):
    with pytest.raises(ValueError):
        MODULE._parse_generation(content)


def test_validate_dependencies_rejects_specifiers():
    with pytest.raises(ValueError):
        MODULE._validate_dependencies(["mutagen==1.0"])
    with pytest.raises(ValueError):
        MODULE._validate_dependencies(["evil; rm -rf /"])


def test_derived_image_tag_is_stable_and_order_independent():
    tag1 = MODULE._derived_image_tag("pfind-search-paths:latest", ["a", "b"])
    tag2 = MODULE._derived_image_tag("pfind-search-paths:latest", ["b", "a"])
    assert tag1 == tag2
    assert tag1.startswith("pfind-search-paths:deps-")


def test_whitelist_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("PFIND_WHITELIST", str(tmp_path / "whitelist.json"))
    assert "rarfile" not in MODULE.load_whitelist()
    assert "mutagen" in MODULE.load_whitelist()  # built-in default
    MODULE.approve_packages(["rarfile"])
    assert "rarfile" in MODULE.load_whitelist()


def test_build_worker_image_returns_base_when_no_dependencies():
    with patch.object(MODULE, "build_image") as build:
        assert MODULE.build_worker_image("base:latest") == "base:latest"
    build.assert_called_once()


def test_build_worker_image_builds_derived_for_dependencies():
    built = MODULE.subprocess.CompletedProcess(args=[], returncode=0)
    with (
        patch.object(MODULE, "build_image"),
        patch.object(MODULE, "_image_exists", return_value=False),
        patch.object(MODULE, "_run_docker", return_value=built) as run,
    ):
        tag = MODULE.build_worker_image("base:latest", ["mutagen"])

    assert tag.startswith("base:deps-")
    command = run.call_args.args[0]
    assert command[0:2] == ["docker", "build"]


def test_cli_no_deps_rejects_packages(tmp_path):
    from typer.testing import CliRunner

    (tmp_path / "file.txt").write_text("content")
    runner = CliRunner()
    with (
        patch.object(cli.backend, "check_docker_available"),
        patch.object(
            cli.backend,
            "generate_filter",
            return_value=_gen("def filter_paths(paths): return paths", ["rarfile"]),
        ),
        patch.object(cli.backend, "build_worker_image") as build,
        patch.object(cli.backend, "load_whitelist", return_value=set()),
    ):
        result = runner.invoke(cli.app, ["files", str(tmp_path), "--no-deps"])

    assert result.exit_code == 1
    assert "rarfile" in result.output
    build.assert_not_called()


def test_cli_yes_approves_packages(tmp_path):
    from typer.testing import CliRunner

    (tmp_path / "file.txt").write_text("content")
    runner = CliRunner()
    with (
        patch.object(cli.backend, "check_docker_available"),
        patch.object(
            cli.backend,
            "generate_filter",
            return_value=_gen("def filter_paths(paths): return paths", ["rarfile"]),
        ),
        patch.object(cli.backend, "build_worker_image", return_value="img:deps"),
        patch.object(cli.backend, "run_filter", return_value=[]),
        patch.object(cli.backend, "load_whitelist", return_value=set()),
        patch.object(cli.backend, "approve_packages") as persist,
    ):
        result = runner.invoke(cli.app, ["files", str(tmp_path), "--yes"])

    assert result.exit_code == 0
    persist.assert_called_once_with(["rarfile"], "python")


def test_highlight_returns_plain_text_when_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    code = "def filter_paths(paths): return paths"
    assert cli._highlight(code) == code


def test_highlight_returns_plain_text_when_not_a_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(cli.sys, "stderr", Mock(isatty=lambda: False))
    code = "def filter_paths(paths): return paths"
    assert cli._highlight(code) == code


def test_highlight_adds_ansi_when_color_enabled(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(cli.sys, "stderr", Mock(isatty=lambda: True))
    highlighted = cli._highlight("def filter_paths(paths): return paths")
    assert "\x1b[" in highlighted


def test_cli_save_writes_generated_code(tmp_path):
    from typer.testing import CliRunner

    (tmp_path / "file.txt").write_text("content")
    out = tmp_path / "filter.py"
    runner = CliRunner()
    with (
        patch.object(cli.backend, "check_docker_available"),
        patch.object(cli.backend, "build_worker_image", return_value=cli.backend.DEFAULT_IMAGE),
        patch.object(
            cli.backend, "generate_filter", return_value=_gen("def filter_paths(paths): return []")
        ),
        patch.object(cli.backend, "run_filter", return_value=[]),
    ):
        result = runner.invoke(cli.app, ["files", str(tmp_path), "--save", str(out)])

    assert result.exit_code == 0
    assert out.read_text() == "def filter_paths(paths): return []"


def _invoke_with_results(args, records):
    from typer.testing import CliRunner

    runner = CliRunner()
    with patch.object(cli.backend, "search", return_value=records):
        return runner.invoke(cli.app, ["prompt", "/tmp", *args])


def test_cli_default_prints_paths_only():
    result = _invoke_with_results([], [{"path": "/a", "lines": 5}, {"path": "/b"}])
    assert result.exit_code == 0
    assert result.output == "/a\n/b\n"


def test_cli_verbose_shows_extra_fields():
    result = _invoke_with_results(["--verbose"], [{"path": "/a", "lines": 5}, {"path": "/b"}])
    assert result.exit_code == 0
    assert "/a\tlines=5" in result.output
    assert "/b" in result.output


def test_cli_json_outputs_records():
    result = _invoke_with_results(["--json"], [{"path": "/a", "lines": 5}])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload == {"count": 1, "results": [{"path": "/a", "lines": 5}]}


def test_cli_json_and_verbose_are_mutually_exclusive():
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(cli.app, ["prompt", "/tmp", "--json", "--verbose"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_docker_timeout_kills_plugin_process_group():
    process = Mock(pid=123, returncode=-9)
    process.communicate.side_effect = [
        MODULE.subprocess.TimeoutExpired(["docker", "info"], 1),
        ("", ""),
    ]
    with (
        patch.object(MODULE.subprocess, "Popen", return_value=process),
        patch.object(MODULE.os, "name", "posix"),
        patch.object(MODULE.os, "killpg") as killpg,
        pytest.raises(MODULE.subprocess.TimeoutExpired),
    ):
        MODULE._run_docker(["docker", "info"], timeout=1, capture_output=True)

    killpg.assert_called_once_with(123, MODULE.signal.SIGKILL)


def test_cli_prints_clean_docker_error():
    from typer.testing import CliRunner

    runner = CliRunner()
    with patch.object(cli.backend, "search", side_effect=MODULE.DockerUnavailableError("offline")):
        result = runner.invoke(cli.app, ["files", "/tmp"])

    assert result.exit_code == 1
    assert "error: offline" in result.output


def test_run_filter_uses_security_and_resource_flags(tmp_path):
    completed = MODULE.subprocess.CompletedProcess(
        args=[], returncode=0, stdout=b'{"ok":true,"results":[]}', stderr=b""
    )
    with patch.object(MODULE, "_run_docker", return_value=completed) as run:
        assert MODULE.run_filter("def filter_paths(paths): return []", tmp_path, []) == []

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
