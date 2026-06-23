import ctypes
import ctypes.util
import io
import json
import plistlib
import subprocess
import sys
from unittest.mock import Mock, patch

import pytest

from fakes import FakeSandbox
from nfind import backend as MODULE
from nfind import cli, metadata, worker
from nfind import execution as EXECUTION
from nfind import generation as GENERATION


def _gen(code, dependencies=()):
    return MODULE.GeneratedFilter(code=code, dependencies=list(dependencies))


def _gen_node(code, dependencies=()):
    return MODULE.GeneratedFilter(code=code, dependencies=list(dependencies), runtime="node")


def test_validate_code_shape_accepts_one_filter_function():
    MODULE._validate_code_shape("def filter_paths(paths):\n    return paths")


def test_validate_code_shape_accepts_top_level_imports():
    MODULE._validate_code_shape(
        "import os\n"
        "from pathlib import Path\n\n"
        "def filter_paths(paths):\n"
        "    return [p for p in paths if os.path.isfile(p)]"
    )


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


def test_worker_exposes_macos_meta_as_global():
    payload = {
        "code": (
            "def filter_paths(paths):\n"
            "    return [p for p in paths if META.get(p, {}).get('quarantined')]"
        ),
        "paths": ["/data/a", "/data/b"],
        "meta": {"/data/a": {"quarantined": True}},
    }

    assert MODULE._worker_response(payload) == {"ok": True, "results": [{"path": "/data/a"}]}


def test_worker_meta_defaults_to_empty_dict():
    # Without "meta", META must still be defined so referencing it does not NameError.
    payload = {
        "code": "def filter_paths(paths):\n    return [p for p in paths if META.get(p)]",
        "paths": ["/data/a"],
    }

    assert MODULE._worker_response(payload) == {"ok": True, "results": []}


def test_worker_rejects_non_object_meta():
    payload = {
        "code": "def filter_paths(paths): return paths",
        "paths": ["/data/a"],
        "meta": ["not", "a", "dict"],
    }

    with pytest.raises(ValueError, match="meta"):
        MODULE._worker_response(payload)


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


def test_execute_worker_main_writes_response_file(tmp_path, monkeypatch):
    response = tmp_path / "response.json"
    payload = {"code": "def filter_paths(paths): return paths", "paths": ["/data/a"]}
    monkeypatch.setattr(MODULE.sys, "stdin", io.StringIO(json.dumps(payload)))

    assert MODULE.execute_worker_main(str(response)) == 0
    assert json.loads(response.read_text()) == {"ok": True, "results": [{"path": "/data/a"}]}


def test_execute_worker_main_records_error_for_bad_request(tmp_path, monkeypatch):
    response = tmp_path / "response.json"
    monkeypatch.setattr(MODULE.sys, "stdin", io.StringIO("not json"))

    assert MODULE.execute_worker_main(str(response)) == 0
    written = json.loads(response.read_text())
    assert written["ok"] is False
    assert "error" in written


def test_execute_worker_main_truncates_oversized_response(tmp_path, monkeypatch):
    response = tmp_path / "response.json"
    payload = {
        "code": "def filter_paths(paths): return paths",
        "paths": [f"/data/{i}" for i in range(1000)],
    }
    monkeypatch.setattr(MODULE.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(worker, "MAX_RESULT_BYTES", 10)

    assert MODULE.execute_worker_main(str(response)) == 0
    assert json.loads(response.read_text()) == {
        "ok": False,
        "error": "filter response exceeded the allowed size",
    }


def test_module_main_dispatches_worker(monkeypatch):
    monkeypatch.setattr(MODULE.sys, "argv", ["worker.py", "--worker"])
    with patch.object(worker, "worker_main", return_value=0) as worker_main:
        assert worker._module_main() == 0
    worker_main.assert_called_once_with()


def test_module_main_dispatches_execute_worker(monkeypatch):
    monkeypatch.setattr(MODULE.sys, "argv", ["worker.py", "--execute-worker", "/tmp/resp"])
    with patch.object(worker, "execute_worker_main", return_value=0) as execute:
        assert worker._module_main() == 0
    execute.assert_called_once_with("/tmp/resp")


def test_module_main_rejects_unknown_invocation(monkeypatch, capsys):
    monkeypatch.setattr(MODULE.sys, "argv", ["worker.py"])
    assert worker._module_main() == 2
    assert "in-container worker" in capsys.readouterr().err


def test_worker_main_relays_child_response(monkeypatch):
    monkeypatch.setattr(MODULE.sys, "stdin", Mock(buffer=Mock(read=lambda: b"{}")))
    out = io.StringIO()
    monkeypatch.setattr(MODULE.sys, "stdout", out)

    def fake_run(args, **kwargs):
        # The child writes its response file; emulate that side effect.
        MODULE.Path(args[args.index("--execute-worker") + 1]).write_text('{"ok":true,"results":[]}')
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(MODULE.subprocess, "run", fake_run)
    assert MODULE.worker_main() == 0
    assert json.loads(out.getvalue()) == {"ok": True, "results": []}


def test_worker_main_reports_nonzero_child_exit(monkeypatch):
    monkeypatch.setattr(MODULE.sys, "stdin", Mock(buffer=Mock(read=lambda: b"{}")))
    out = io.StringIO()
    monkeypatch.setattr(MODULE.sys, "stdout", out)
    monkeypatch.setattr(
        MODULE.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 137),
    )

    assert MODULE.worker_main() == 0
    written = json.loads(out.getvalue())
    assert written["ok"] is False
    assert "137" in written["error"]


def test_run_filter_returns_normalized_results(tmp_path):
    fake = FakeSandbox(stdout=b'{"ok":true,"results":["/data/a"]}')
    results = EXECUTION.run_filter("code", tmp_path, ["/data/a"], sandbox=fake)
    assert results == [{"path": "/data/a"}]
    # The adapter builds the {code, paths, meta} request and mounts the root read-only.
    request, mounts, _ = fake.runs[0]
    assert json.loads(request) == {"code": "code", "paths": ["/data/a"], "meta": {}}
    assert mounts[0].target == "/data" and mounts[0].read_only is True


@pytest.mark.parametrize("bad", [0, -1.5])
def test_run_filter_rejects_nonpositive_limits(tmp_path, bad):
    with pytest.raises(ValueError):
        EXECUTION.run_filter("code", tmp_path, [], sandbox=FakeSandbox(), timeout=bad)


def test_run_filter_maps_timeout_to_timeouterror(tmp_path):
    fake = FakeSandbox(run_error=EXECUTION.SandboxTimeout("boom"))
    with pytest.raises(TimeoutError, match="exceeded"):
        EXECUTION.run_filter("code", tmp_path, [], sandbox=fake, timeout=10)


def test_run_filter_rejects_oversized_output(tmp_path):
    fake = FakeSandbox(run_error=EXECUTION.SandboxOutputTooLarge("too big"))
    with pytest.raises(RuntimeError, match="exceeded the allowed size"):
        EXECUTION.run_filter("code", tmp_path, [], sandbox=fake)


def test_run_filter_reports_nonzero_worker_exit(tmp_path):
    fake = FakeSandbox(stderr=b"boom", returncode=1)
    with pytest.raises(RuntimeError, match="Docker worker failed: boom"):
        EXECUTION.run_filter("code", tmp_path, [], sandbox=fake)


def test_run_filter_rejects_invalid_json(tmp_path):
    fake = FakeSandbox(stdout=b"not json")
    with pytest.raises(RuntimeError, match="invalid response"):
        EXECUTION.run_filter("code", tmp_path, [], sandbox=fake)


def test_run_filter_propagates_worker_error(tmp_path):
    fake = FakeSandbox(stdout=b'{"ok":false,"error":"nope"}')
    with pytest.raises(RuntimeError, match="Generated filter failed: nope"):
        EXECUTION.run_filter("code", tmp_path, [], sandbox=fake)


def test_run_filter_rejects_disallowed_result_path(tmp_path):
    fake = FakeSandbox(stdout=b'{"ok":true,"results":["/evil"]}')
    with pytest.raises(RuntimeError, match="invalid result"):
        EXECUTION.run_filter("code", tmp_path, ["/data/a"], sandbox=fake)


def test_run_filter_accepts_explicit_limits(tmp_path):
    fake = FakeSandbox(stdout=b'{"ok":true,"results":[]}')
    limits = EXECUTION.Limits(memory="64m", cpus=2.0, pids=8, timeout=3.0, max_output_bytes=512)
    EXECUTION.run_filter("code", tmp_path, [], sandbox=fake, limits=limits)
    # The supplied Limits is passed straight through to the sandbox.
    assert fake.runs[0][2] is limits


def test_run_filter_rejects_nonpositive_explicit_limits(tmp_path):
    bad = EXECUTION.Limits(timeout=0)
    with pytest.raises(ValueError):
        EXECUTION.run_filter("code", tmp_path, [], sandbox=FakeSandbox(), limits=bad)


def test_build_worker_image_surfaces_package_names_on_build_failure():
    fake = FakeSandbox(derive_error=EXECUTION.SandboxError("exit status 1"))
    with pytest.raises(EXECUTION.DockerError, match=r"packages \(mutagen, rarfile\)"):
        EXECUTION.build_worker_image("base:latest", ["rarfile", "mutagen"], sandbox=fake)


def test_search_maps_host_paths_in_records(tmp_path):
    (tmp_path / "file.txt").write_text("content")
    with (
        patch.object(MODULE, "check_docker_available"),
        patch.object(EXECUTION, "build_worker_image", return_value=EXECUTION.DEFAULT_IMAGE),
        patch.object(
            MODULE, "generate_filter", return_value=_gen("def filter_paths(paths): return paths")
        ),
        patch.object(
            EXECUTION,
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
        patch.object(EXECUTION, "build_worker_image", return_value=EXECUTION.DEFAULT_IMAGE),
        patch.object(
            MODULE, "generate_filter", return_value=_gen("def filter_paths(paths): return []")
        ),
        patch.object(EXECUTION, "run_filter", return_value=[]) as run_filter,
    ):
        MODULE.search(str(tmp_path), "files", on_generated=seen.append, format_code=False)

    assert [g.code for g in seen] == ["def filter_paths(paths): return []"]
    run_filter.assert_called_once()


def test_search_aborts_when_on_generated_raises(tmp_path):
    (tmp_path / "file.txt").write_text("content")

    def decline(code: str) -> None:
        raise RuntimeError("declined")

    with (
        patch.object(MODULE, "check_docker_available"),
        patch.object(EXECUTION, "build_worker_image", return_value=EXECUTION.DEFAULT_IMAGE),
        patch.object(
            MODULE, "generate_filter", return_value=_gen("def filter_paths(paths): return []")
        ),
        patch.object(EXECUTION, "run_filter") as run_filter,
        pytest.raises(RuntimeError, match="declined"),
    ):
        MODULE.search(str(tmp_path), "files", on_generated=decline)

    run_filter.assert_not_called()


def test_search_rejects_unapproved_dependencies(tmp_path):
    (tmp_path / "file.txt").write_text("content")
    with (
        patch.object(MODULE, "check_docker_available"),
        patch.object(EXECUTION, "build_worker_image") as build,
        patch.object(
            MODULE,
            "generate_filter",
            return_value=_gen("def filter_paths(paths): return paths", ["sketchy-pkg"]),
        ),
        patch.object(EXECUTION, "run_filter") as run_filter,
        pytest.raises(MODULE.DependencyError, match="sketchy-pkg"),
    ):
        MODULE.search(str(tmp_path), "files", whitelist=set())

    build.assert_not_called()
    run_filter.assert_not_called()


def test_search_uses_whitelisted_dependency_without_prompt(tmp_path):
    (tmp_path / "file.txt").write_text("content")
    with (
        patch.object(MODULE, "check_docker_available"),
        patch.object(EXECUTION, "build_worker_image", return_value="img:deps") as build,
        patch.object(
            MODULE,
            "generate_filter",
            return_value=_gen("def filter_paths(paths): return paths", ["mutagen"]),
        ),
        patch.object(EXECUTION, "run_filter", return_value=[]) as run_filter,
        patch.object(EXECUTION, "approve_packages") as persist,
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
        patch.object(EXECUTION, "build_worker_image", return_value="img:deps") as build,
        patch.object(
            MODULE,
            "generate_filter",
            return_value=_gen("def filter_paths(paths): return paths", ["rarfile"]),
        ),
        patch.object(EXECUTION, "run_filter", return_value=[]),
        patch.object(EXECUTION, "approve_packages") as persist,
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
        patch.object(EXECUTION, "build_worker_image", return_value=EXECUTION.DEFAULT_IMAGE),
        patch.object(
            cli.backend, "generate_filter", return_value=_gen("def filter_paths(paths): return []")
        ),
        patch.object(EXECUTION, "run_filter") as run_filter,
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


def test_check_undefined_names_accepts_top_level_import():
    MODULE._check_undefined_names(
        "import os\n\ndef filter_paths(paths):\n    return [p for p in paths if os.path.isfile(p)]"
    )


def test_check_undefined_names_rejects_missing_import():
    # `os` is used but never imported at the top level -- the bug this gate exists to catch.
    with pytest.raises(ValueError, match=r"undefined name.*'os'"):
        MODULE._check_undefined_names(
            "def filter_paths(paths):\n    return [p for p in paths if os.path.isfile(p)]"
        )


def test_check_undefined_names_allows_injected_meta_global():
    code = "def filter_paths(paths):\n    return [p for p in paths if META.get(p, {}).get('q')]"
    with pytest.raises(ValueError, match="META"):
        MODULE._check_undefined_names(code)  # without the builtin declared, META is undefined
    MODULE._check_undefined_names(code, extra_builtins=("META",))  # declared -> accepted


def test_check_undefined_names_fails_open_without_ruff():
    # No ruff -> the gate must not block generation on a tooling gap.
    with patch.object(GENERATION, "_ruff_path", return_value=None):
        MODULE._check_undefined_names("def filter_paths(paths):\n    return os.listdir(paths)")


def test_parse_generation_rejects_filter_using_undefined_name():
    content = json.dumps(
        {"code": "def filter_paths(paths):\n    return [p for p in paths if re.match('x', p)]"}
    )
    with pytest.raises(ValueError, match=r"undefined name.*'re'"):
        MODULE._parse_generation(content)


def test_parse_generation_whitelists_meta_when_macos_meta_enabled():
    content = json.dumps(
        {"code": "def filter_paths(paths):\n    return [p for p in paths if META.get(p)]"}
    )
    with pytest.raises(ValueError, match="META"):
        MODULE._parse_generation(content)  # META undefined without metadata in play
    result = MODULE._parse_generation(content, macos_meta=True)
    assert result.runtime == "python"


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

    def fake_build(image, dependencies, *, runtime, rebuild, build_timeout, sandbox=None):
        captured["image"] = image
        captured["runtime"] = runtime.name
        return "nfind-search-node:deps-x"

    with (
        patch.object(MODULE, "check_docker_available"),
        patch.object(
            MODULE,
            "generate_filter",
            return_value=_gen_node("function filterPaths(p){return p;}", ["ts-morph"]),
        ),
        patch.object(EXECUTION, "build_worker_image", side_effect=fake_build),
        patch.object(EXECUTION, "run_filter", return_value=[]) as run_filter,
    ):
        MODULE.search(str(tmp_path), "typescript files")

    assert captured == {"image": MODULE.DEFAULT_NODE_IMAGE, "runtime": "node"}
    assert run_filter.call_args.kwargs["image"] == "nfind-search-node:deps-x"


def test_derived_dockerfile_uses_pip_or_npm():
    py = MODULE.PYTHON_RUNTIME.derived_dockerfile("base", ["mutagen"])
    assert "pip install" in py and "USER worker" in py
    node = MODULE.NODE_RUNTIME.derived_dockerfile("base", ["ts-morph"])
    assert "npm install" in node and "USER node" in node


def test_whitelist_is_per_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("NFIND_WHITELIST", str(tmp_path / "wl.json"))
    MODULE.approve_packages(["rarfile"], "python")
    MODULE.approve_packages(["left-pad"], "node")
    assert "rarfile" in MODULE.load_whitelist("python")
    assert "rarfile" not in MODULE.load_whitelist("node")
    assert "left-pad" in MODULE.load_whitelist("node")
    assert "ts-morph" in MODULE.load_whitelist("node")  # node default


def test_whitelist_path_prefers_nfind_whitelist_override(tmp_path, monkeypatch):
    override = tmp_path / "custom" / "wl.json"
    monkeypatch.setenv("NFIND_WHITELIST", str(override))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert MODULE._whitelist_path() == override


def test_whitelist_path_uses_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.delenv("NFIND_WHITELIST", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert MODULE._whitelist_path() == tmp_path / "xdg" / "nfind" / "whitelist.json"


def test_whitelist_path_falls_back_to_home_config(tmp_path, monkeypatch):
    monkeypatch.delenv("NFIND_WHITELIST", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(MODULE.Path, "home", classmethod(lambda cls: tmp_path))
    assert MODULE._whitelist_path() == tmp_path / ".config" / "nfind" / "whitelist.json"


def test_pip_dependencies_are_pep503_normalized():
    # Underscores, dots, and case collapse to one canonical dash-lowercase form.
    assert MODULE.PYTHON_RUNTIME.validate_packages(
        ["Tree_Sitter_Python", "tree-sitter-python", "tree.sitter.python"]
    ) == ["tree-sitter-python"]


def test_npm_dependencies_lowercase_but_keep_separators():
    # npm treats - and _ as distinct, so only case is normalized.
    assert MODULE.NODE_RUNTIME.validate_packages(["Ts-Morph", "left_pad"]) == [
        "left_pad",
        "ts-morph",
    ]


def test_load_whitelist_self_heals_non_normalized_names(tmp_path, monkeypatch):
    path = tmp_path / "wl.json"
    monkeypatch.setenv("NFIND_WHITELIST", str(path))
    # A legacy file with an underscore variant of a dash package.
    path.write_text(json.dumps({"python": ["tree_sitter_language_pack", "Mutagen"]}))

    loaded = MODULE.load_whitelist("python")

    assert "tree-sitter-language-pack" in loaded  # canonicalized
    assert "tree_sitter_language_pack" not in loaded  # underscore form gone
    assert "mutagen" in loaded


def test_approve_packages_writes_canonical_names(tmp_path, monkeypatch):
    path = tmp_path / "wl.json"
    monkeypatch.setenv("NFIND_WHITELIST", str(path))
    MODULE.approve_packages(["Tree_Sitter_Go"], "python")

    saved = json.loads(path.read_text())
    assert saved["python"] == ["tree-sitter-go"]


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


def _fake_openai(*contents):
    """Patch the generation client to return the given reply contents in order.

    Patches ``_make_client`` so the tests exercise the generate/retry logic without
    needing real provider credentials.
    """
    responses = [Mock(choices=[Mock(message=Mock(content=content))]) for content in contents]
    client = Mock()
    client.chat.completions.create.side_effect = responses
    return patch.object(GENERATION, "_make_client", return_value=client), client


def test_split_model_defaults_to_openai_for_bare_name():
    assert GENERATION._split_model("gpt-4o-mini") == ("openai", "gpt-4o-mini")


def test_split_model_parses_provider_prefix():
    assert GENERATION._split_model("anthropic/claude-3-5-sonnet") == (
        "anthropic",
        "claude-3-5-sonnet",
    )
    # Only the first slash splits; vendor-qualified names pass through.
    assert GENERATION._split_model("openrouter/anthropic/claude-3") == (
        "openrouter",
        "anthropic/claude-3",
    )
    # Stray whitespace and an empty half fall back to the default provider.
    assert GENERATION._split_model("  groq/llama-3.3  ") == ("groq", "llama-3.3")
    assert GENERATION._split_model("/oops") == ("openai", "/oops")


def test_make_client_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unknown model provider"):
        GENERATION._make_client("nope")


def test_make_client_missing_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        GENERATION._make_client("anthropic")


def test_make_client_uses_base_url_and_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "secret")
    with patch("openai.OpenAI") as ctor:
        GENERATION._make_client("groq")
    assert ctor.call_args.kwargs == {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "secret",
    }


def test_make_client_local_provider_needs_no_key(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    with patch("openai.OpenAI") as ctor:
        GENERATION._make_client("ollama")
    assert ctor.call_args.kwargs["base_url"] == "http://localhost:11434/v1"
    assert ctor.call_args.kwargs["api_key"]  # non-empty placeholder


@pytest.mark.parametrize(
    "content",
    [
        '{"code": "x"}',
        '```json\n{"code": "x"}\n```',
        'Here you go:\n```\n{"code": "x"}\n```',
        'Sure! {"code": "x"} hope that helps',
    ],
)
def test_extract_json_object_recovers_object(content):
    assert json.loads(GENERATION._extract_json_object(content)) == {"code": "x"}


def test_generate_filter_drops_json_mode_when_provider_rejects_it():
    good = json.dumps({"code": "def filter_paths(paths): return paths"})
    client = Mock()
    # First call (with response_format) errors; the retry without it succeeds.
    client.chat.completions.create.side_effect = [
        Exception("response_format not supported"),
        Mock(choices=[Mock(message=Mock(content=good))]),
    ]
    with patch.object(GENERATION, "_make_client", return_value=client):
        result = GENERATION.generate_filter("anything", model="groq/llama-3.3")
    assert result.code == "def filter_paths(paths): return paths"
    assert client.chat.completions.create.call_count == 2
    first, second = client.chat.completions.create.call_args_list
    assert "response_format" in first.kwargs
    assert "response_format" not in second.kwargs
    assert first.kwargs["model"] == "llama-3.3"  # provider prefix stripped


def _good_response():
    good = json.dumps({"code": "def filter_paths(paths): return paths"})
    return Mock(choices=[Mock(message=Mock(content=good))])


def test_generate_filter_renames_max_tokens_for_reasoning_models():
    client = Mock()
    client.chat.completions.create.side_effect = [
        Exception(
            "Unsupported parameter: 'max_tokens' is not supported. Use 'max_completion_tokens'."
        ),
        _good_response(),
    ]
    with patch.object(GENERATION, "_make_client", return_value=client):
        result = GENERATION.generate_filter("anything", model="openai/o3")
    assert result.code
    last = client.chat.completions.create.call_args_list[-1].kwargs
    assert "max_completion_tokens" in last and "max_tokens" not in last


def test_generate_filter_drops_unsupported_temperature():
    client = Mock()
    client.chat.completions.create.side_effect = [
        Exception("Unsupported value: 'temperature' does not support 0; only the default (1)."),
        _good_response(),
    ]
    with patch.object(GENERATION, "_make_client", return_value=client):
        GENERATION.generate_filter("anything", model="o3")
    assert "temperature" not in client.chat.completions.create.call_args_list[-1].kwargs


def test_generate_filter_learned_adaptation_persists_across_attempts():
    # The max_tokens fix discovered on attempt 1 must be reused on the retry, not relearned.
    client = Mock()
    client.chat.completions.create.side_effect = [
        Exception("'max_tokens' is not supported; use 'max_completion_tokens'."),
        Mock(choices=[Mock(message=Mock(content="not json"))]),  # triggers a validation retry
        _good_response(),
    ]
    with patch.object(GENERATION, "_make_client", return_value=client):
        GENERATION.generate_filter("anything", model="o3", attempts=2)
    assert client.chat.completions.create.call_count == 3
    # Every successful (non-error) call used the renamed parameter.
    for call in client.chat.completions.create.call_args_list[1:]:
        assert "max_completion_tokens" in call.kwargs and "max_tokens" not in call.kwargs


def test_generate_filter_reports_unknown_model():
    class NotFound(Exception):
        status_code = 404

    client = Mock()
    client.chat.completions.create.side_effect = NotFound(
        "The model `gpt-5.0` does not exist or you do not have access to it."
    )
    with (
        patch.object(GENERATION, "_make_client", return_value=client),
        pytest.raises(RuntimeError, match="not found.*--list-models"),
    ):
        GENERATION.generate_filter("anything", model="openai/gpt-5.0")
    # Reported immediately, without burning retries on a doomed id.
    assert client.chat.completions.create.call_count == 1


def _good_responses_result():
    good = json.dumps({"code": "def filter_paths(paths): return paths"})
    return Mock(output_text=good)


def test_generate_filter_falls_back_to_responses_api():
    # Codex/reasoning models reject chat-completions with a 404 pointing to v1/responses.
    client = Mock()
    client.chat.completions.create.side_effect = Exception(
        "This model is only supported in v1/responses and not in v1/chat/completions."
    )
    client.responses.create.return_value = _good_responses_result()
    with patch.object(GENERATION, "_make_client", return_value=client):
        result = GENERATION.generate_filter("anything", model="openai/gpt-5.1-codex-mini")
    assert result.code
    # Switched endpoints rather than reporting the model as missing.
    assert client.responses.create.call_count == 1
    sent = client.responses.create.call_args.kwargs
    assert sent["model"] == "gpt-5.1-codex-mini"  # provider prefix stripped
    assert sent["input"] and "max_output_tokens" in sent


def test_generate_filter_responses_switch_persists_across_attempts():
    # Once switched to the Responses API, the retry must not go back to chat-completions.
    client = Mock()
    client.chat.completions.create.side_effect = Exception(
        "This model is only supported in v1/responses and not in v1/chat/completions."
    )
    client.responses.create.side_effect = [
        Mock(output_text="not json"),  # triggers a validation retry
        _good_responses_result(),
    ]
    with patch.object(GENERATION, "_make_client", return_value=client):
        GENERATION.generate_filter("anything", model="openai/gpt-5.1-codex-mini", attempts=2)
    # Only the one probe that triggered the switch hit chat-completions.
    assert client.chat.completions.create.call_count == 1
    assert client.responses.create.call_count == 2


def test_generate_filter_caches_responses_verdict():
    # The probe that reveals a Responses-only model records the verdict for next time.
    client = Mock()
    client.chat.completions.create.side_effect = Exception(
        "This model is only supported in v1/responses and not in v1/chat/completions."
    )
    client.responses.create.return_value = _good_responses_result()
    with patch.object(GENERATION, "_make_client", return_value=client):
        GENERATION.generate_filter("anything", model="openai/gpt-5.1-codex-mini")
    # Cached under the full selector, not the bare model name.
    assert GENERATION.get_endpoint("openai/gpt-5.1-codex-mini") == "responses"


def test_generate_filter_uses_cached_responses_verdict():
    # A cached verdict skips the throwaway chat-completions probe entirely.
    GENERATION.set_endpoint("openai/gpt-5.1-codex-mini", "responses")
    client = Mock()
    client.responses.create.return_value = _good_responses_result()
    with patch.object(GENERATION, "_make_client", return_value=client):
        result = GENERATION.generate_filter("anything", model="openai/gpt-5.1-codex-mini")
    assert result.code
    client.chat.completions.create.assert_not_called()
    assert client.responses.create.call_count == 1


def test_list_models_returns_sorted_ids():
    client = Mock()
    client.models.list.return_value = Mock(data=[Mock(id="gpt-4o-mini"), Mock(id="gpt-4o")])
    with patch.object(GENERATION, "_make_client", return_value=client) as make:
        assert GENERATION.list_models("openai/whatever") == ["gpt-4o", "gpt-4o-mini"]
    assert make.call_args.args[0] == "openai"  # provider taken from the selector


def test_list_models_uses_selected_provider():
    client = Mock()
    client.models.list.return_value = Mock(data=[])
    with patch.object(GENERATION, "_make_client", return_value=client) as make:
        GENERATION.list_models("groq/llama-3.3")
    assert make.call_args.args[0] == "groq"


def test_list_models_reports_unsupported_listing():
    client = Mock()
    client.models.list.side_effect = Exception("listing not available")
    with (
        patch.object(GENERATION, "_make_client", return_value=client),
        pytest.raises(RuntimeError, match="Could not list models"),
    ):
        GENERATION.list_models("groq/x")


def test_generate_filter_succeeds_on_first_attempt():
    good = json.dumps({"code": "def filter_paths(paths): return paths"})
    patcher, client = _fake_openai(good)
    with patcher:
        result = GENERATION.generate_filter("anything")
    assert result.code == "def filter_paths(paths): return paths"
    assert client.chat.completions.create.call_count == 1


def test_generate_filter_retries_on_invalid_then_succeeds():
    good = json.dumps({"code": "def filter_paths(paths): return paths"})
    patcher, client = _fake_openai("not json", good)
    retries = []
    with patcher:
        result = GENERATION.generate_filter("anything", on_retry=lambda n, exc: retries.append(n))
    assert result.code == "def filter_paths(paths): return paths"
    assert client.chat.completions.create.call_count == 2
    assert retries == [1]
    # The corrective message is fed back before retrying.
    second_call = client.chat.completions.create.call_args_list[1]
    messages = second_call.kwargs["messages"]
    assert messages[-2]["role"] == "assistant" and messages[-2]["content"] == "not json"
    assert messages[-1]["role"] == "user"
    # Retries leave temperature 0 behind so the model diverges.
    assert second_call.kwargs["temperature"] == GENERATION._RETRY_TEMPERATURE


def test_generate_filter_retries_on_undefined_name_then_succeeds():
    body = "def filter_paths(paths):\n    return [p for p in paths if os.stat(p)]"
    bad = json.dumps({"code": body})
    good = json.dumps({"code": f"import os\n\n{body}"})
    patcher, client = _fake_openai(bad, good)
    retries = []
    with patcher:
        result = GENERATION.generate_filter("anything", on_retry=lambda n, exc: retries.append(exc))
    assert "import os" in result.code
    assert client.chat.completions.create.call_count == 2
    assert retries and "undefined name" in str(retries[0])


def test_generate_filter_raises_after_exhausting_attempts():
    patcher, client = _fake_openai("not json", "still not json")
    with patcher, pytest.raises(ValueError, match="after 2 attempt"):
        GENERATION.generate_filter("anything", attempts=2)
    assert client.chat.completions.create.call_count == 2


def test_generate_filter_rejects_nonpositive_attempts():
    with pytest.raises(ValueError, match="attempts must be at least 1"):
        GENERATION.generate_filter("anything", attempts=0)


def test_generate_filter_appends_macos_meta_guidance_only_when_enabled():
    good = json.dumps({"code": "def filter_paths(paths): return paths"})

    patcher, client = _fake_openai(good)
    with patcher:
        GENERATION.generate_filter("anything", macos_meta=True)
    system = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert "META" in system and "quarantined" in system

    patcher, client = _fake_openai(good)
    with patcher:
        GENERATION.generate_filter("anything")
    system = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert "META" not in system


def test_collect_macos_metadata_empty_off_darwin(monkeypatch):
    monkeypatch.setattr(metadata.sys, "platform", "linux")
    assert MODULE.collect_macos_metadata({"/data/a": "/host/a"}) == {}


@pytest.mark.skipif(sys.platform != "darwin", reason="reads macOS extended attributes")
def test_collect_macos_metadata_reads_tags_and_quarantine(tmp_path):
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.setxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]

    def setxattr(path, name, value):
        rc = libc.setxattr(str(path).encode(), name.encode(), value, len(value), 0, 0)
        assert rc == 0, ctypes.get_errno()

    tagged = tmp_path / "tagged.txt"
    tagged.write_text("x")
    setxattr(
        tagged, metadata._XATTR_TAGS, plistlib.dumps(["Red\n6", "Work"], fmt=plistlib.FMT_BINARY)
    )
    setxattr(tagged, metadata._XATTR_QUARANTINE, b"0083;0;Safari;")
    setxattr(
        tagged,
        metadata._XATTR_WHERE_FROMS,
        plistlib.dumps(["https://example.com/x"], fmt=plistlib.FMT_BINARY),
    )
    plain = tmp_path / "plain.txt"
    plain.write_text("x")

    meta = MODULE.collect_macos_metadata(
        {"/data/tagged.txt": str(tagged), "/data/plain.txt": str(plain)}
    )

    assert meta == {
        "/data/tagged.txt": {
            "tags": ["Red", "Work"],
            "quarantined": True,
            "where_froms": ["https://example.com/x"],
        }
    }


def test_imply_packages_adds_tree_sitter_core_for_grammar_wheels():
    # A grammar wheel alone leaves `import tree_sitter` failing; core must be added.
    assert MODULE._imply_packages("python", ["tree-sitter-go"]) == ["tree-sitter", "tree-sitter-go"]
    # Already present: unchanged (deduplicated/sorted).
    assert MODULE._imply_packages("python", ["tree-sitter", "tree-sitter-go"]) == [
        "tree-sitter",
        "tree-sitter-go",
    ]
    # No grammar wheel: untouched.
    assert MODULE._imply_packages("python", ["mutagen"]) == ["mutagen"]
    # Node runtime: the Python implication does not apply.
    assert MODULE._imply_packages("node", ["tree-sitter-go"]) == ["tree-sitter-go"]


def test_validate_dependencies_rejects_specifiers():
    with pytest.raises(ValueError):
        MODULE._validate_dependencies(["mutagen==1.0"])
    with pytest.raises(ValueError):
        MODULE._validate_dependencies(["evil; rm -rf /"])


def test_build_worker_image_derived_dockerfile_is_order_independent():
    # The derived image is content-addressed on the Dockerfile text, and packages are
    # sorted before the Dockerfile is rendered, so dependency order does not matter.
    first = FakeSandbox()
    EXECUTION.build_worker_image("base:latest", ["a", "b"], sandbox=first)
    second = FakeSandbox()
    EXECUTION.build_worker_image("base:latest", ["b", "a"], sandbox=second)
    assert first.derive_calls == second.derive_calls


def test_whitelist_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("NFIND_WHITELIST", str(tmp_path / "whitelist.json"))
    assert "rarfile" not in MODULE.load_whitelist()
    assert "mutagen" in MODULE.load_whitelist()  # built-in default
    assert "tree-sitter-python" in MODULE.load_whitelist()  # multi-language parsing
    MODULE.approve_packages(["rarfile"])
    assert "rarfile" in MODULE.load_whitelist()


def test_kotlin_swift_dart_grammars_are_pre_approved():
    # Standalone wheels that bundle their grammar offline, like the other languages.
    assert {"tree-sitter-kotlin", "tree-sitter-swift", "tree-sitter-dart"} <= (
        MODULE.DEFAULT_ALLOWED_PACKAGES
    )


def test_build_worker_image_returns_base_when_no_dependencies():
    fake = FakeSandbox()
    assert EXECUTION.build_worker_image("base:latest", sandbox=fake) == "base:latest"
    assert fake.ensure_calls == [False]
    assert fake.derive_calls == []


def test_build_worker_image_builds_derived_for_dependencies():
    fake = FakeSandbox(derived="base:deps-abc123")
    tag = EXECUTION.build_worker_image("base:latest", ["mutagen"], sandbox=fake)

    assert tag == "base:deps-abc123"
    assert fake.ensure_calls == [False]
    # The derived image is built from the runtime's pip-install Dockerfile.
    assert "pip install" in fake.derive_calls[0]


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
        patch.object(EXECUTION, "build_worker_image") as build,
        patch.object(EXECUTION, "load_whitelist", return_value=set()),
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
        patch.object(EXECUTION, "build_worker_image", return_value="img:deps"),
        patch.object(EXECUTION, "run_filter", return_value=[]),
        patch.object(EXECUTION, "load_whitelist", return_value=set()),
        patch.object(EXECUTION, "approve_packages") as persist,
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
        patch.object(EXECUTION, "build_worker_image", return_value=EXECUTION.DEFAULT_IMAGE),
        patch.object(
            cli.backend, "generate_filter", return_value=_gen("def filter_paths(paths): return []")
        ),
        patch.object(EXECUTION, "run_filter", return_value=[]),
    ):
        result = runner.invoke(cli.app, ["files", str(tmp_path), "--save", str(out)])

    assert result.exit_code == 0
    written = out.read_text()
    assert "def filter_paths(paths):" in written  # ruff may reflow the body onto its own line
    compile(written, "saved.py", "exec")  # the saved script is valid Python


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


def test_cli_list_models_prints_ids():
    from typer.testing import CliRunner

    runner = CliRunner()
    with patch.object(cli.backend, "list_models", return_value=["gpt-4o", "gpt-4o-mini"]) as lm:
        result = runner.invoke(cli.app, ["--list-models"])

    assert result.exit_code == 0
    assert result.output == "gpt-4o\ngpt-4o-mini\n"
    assert lm.call_args.args[0] == cli.backend.DEFAULT_MODEL  # default provider/model


def test_cli_list_models_uses_selected_model_provider():
    from typer.testing import CliRunner

    runner = CliRunner()
    with patch.object(cli.backend, "list_models", return_value=[]) as lm:
        result = runner.invoke(cli.app, ["--list-models", "--model", "groq/llama-3.3"])

    assert result.exit_code == 0
    assert lm.call_args.args[0] == "groq/llama-3.3"


def test_cli_list_models_reports_error():
    from typer.testing import CliRunner

    runner = CliRunner()
    with patch.object(cli.backend, "list_models", side_effect=RuntimeError("no listing here")):
        result = runner.invoke(cli.app, ["--list-models"])

    assert result.exit_code == 1
    assert "no listing here" in result.output


def test_cli_prints_clean_docker_error():
    from typer.testing import CliRunner

    runner = CliRunner()
    with patch.object(cli.backend, "search", side_effect=MODULE.DockerUnavailableError("offline")):
        result = runner.invoke(cli.app, ["files", "/tmp"])

    assert result.exit_code == 1
    assert "error: offline" in result.output


# --- ruff cleanup of generated code ---------------------------------------------


def test_format_generated_code_removes_unused_imports_and_formats():
    messy = (
        "def filter_paths(paths):\n"
        "    import os\n"
        "    import sys\n"
        "    return [p for p in paths if p.endswith('.epub')]\n"
    )
    cleaned = MODULE._format_generated_code(messy, "python")

    assert "import os" not in cleaned
    assert "import sys" not in cleaned
    assert 'endswith(".epub")' in cleaned  # ruff format normalizes quotes
    MODULE._validate_code_shape(cleaned)  # still a valid single-function filter


def test_format_generated_code_wraps_at_configured_line_length():
    # A body line of 89-100 chars wraps at ruff's default (88) but stays whole at 100.
    body = '    return [p for p in paths if p.endswith(".epub") or p.endswith(".mobi") or p.endswith(".azw")]'  # noqa: E501
    assert MODULE.FILTER_LINE_LENGTH == 100
    assert 88 < len(body) <= MODULE.FILTER_LINE_LENGTH
    cleaned = MODULE._format_generated_code(f"def filter_paths(paths):\n{body}\n", "python")
    assert body in cleaned  # left on a single line, so line-length 100 took effect


def test_format_generated_code_leaves_node_unchanged():
    code = "function filterPaths(paths){ return paths; }"
    assert MODULE._format_generated_code(code, "node") == code


def test_format_generated_code_falls_back_when_ruff_missing():
    code = "def filter_paths(paths):\n    import os\n    return paths\n"
    with patch.object(GENERATION, "_ruff_path", return_value=None):
        assert MODULE._format_generated_code(code, "python") == code


def test_search_formats_generated_code_before_running(tmp_path):
    (tmp_path / "file.txt").write_text("content")
    messy = "def filter_paths(paths):\n    import os\n    return paths\n"
    with (
        patch.object(MODULE, "check_docker_available"),
        patch.object(EXECUTION, "build_worker_image", return_value=EXECUTION.DEFAULT_IMAGE),
        patch.object(MODULE, "generate_filter", return_value=_gen(messy)),
        patch.object(EXECUTION, "run_filter", return_value=[]) as run_filter,
    ):
        MODULE.search(str(tmp_path), "files")

    # The code handed to the sandbox is the cleaned version.
    assert "import os" not in run_filter.call_args.args[0]


def test_search_skips_formatting_when_disabled(tmp_path):
    (tmp_path / "file.txt").write_text("content")
    messy = "def filter_paths(paths):\n    import os\n    return paths\n"
    with (
        patch.object(MODULE, "check_docker_available"),
        patch.object(EXECUTION, "build_worker_image", return_value=EXECUTION.DEFAULT_IMAGE),
        patch.object(MODULE, "generate_filter", return_value=_gen(messy)),
        patch.object(EXECUTION, "run_filter", return_value=[]) as run_filter,
    ):
        MODULE.search(str(tmp_path), "files", format_code=False)

    assert "import os" in run_filter.call_args.args[0]


# --- saved filters: render / parse / replay -------------------------------------


def test_serialize_filter_python_is_valid_pep723_script():
    code = "def filter_paths(paths):\n    import mutagen\n    return paths"
    src = MODULE.serialize_filter(_gen(code, ["mutagen"]), "MP3 files", "gpt-4o-mini")

    # Valid Python and a parseable PEP 723 block declaring the dependency.
    compile(src, "saved.py", "exec")
    match = MODULE._SCRIPT_METADATA_RE.search(src)
    assert match is not None
    assert '"mutagen"' in match.group("body")
    # Docstring carries the prompt and the safety warning; code is included verbatim.
    assert "Prompt:  MP3 files" in src
    assert "OUTSIDE the nfind Docker sandbox" in src
    assert "def filter_paths(paths):" in src
    assert 'if __name__ == "__main__":' in src


def test_serialize_filter_wraps_header_within_line_length():
    long_code = "def filter_paths(paths):\n    return paths"
    for runtime in ("python", "node"):
        gen = MODULE.GeneratedFilter(code=long_code, dependencies=[], runtime=runtime)
        src = MODULE.serialize_filter(gen, "epub archives", "gpt-4o-mini")
        longest = max(len(line) for line in src.splitlines())
        assert longest <= MODULE.FILTER_LINE_LENGTH, (runtime, longest)
        # The warning prose is wrapped across multiple lines, not left as one long line.
        assert sum("OUTSIDE the" in line or "trust." in line for line in src.splitlines()) >= 1


def test_serialize_filter_escapes_triple_quotes_in_prompt():
    src = MODULE.serialize_filter(
        _gen("def filter_paths(paths): return paths"), 'a """ quote', "gpt-4o-mini"
    )
    compile(src, "saved.py", "exec")


def test_serialize_filter_node_has_comment_header_and_raw_code():
    code = "function filterPaths(paths){ return paths; }"
    src = MODULE.serialize_filter(_gen_node(code, ["ts-morph"]), "TS files", "gpt-4o-mini")

    assert src.startswith("// nfind filter")
    assert "// Prompt:  TS files" in src
    assert '// nfind-metadata: {"runtime":"node","dependencies":["ts-morph"]}' in src
    assert "python-only" in src
    assert code in src
    assert "# /// script" not in src  # no PEP 723 block for node


def test_saved_filter_standalone_harness_runs(tmp_path):
    (tmp_path / "a.mp3").write_text("x")
    (tmp_path / "b.txt").write_text("x")
    code = 'def filter_paths(paths):\n    return [p for p in paths if p.endswith(".mp3")]'
    script = tmp_path / "mp3.py"
    script.write_text(MODULE.serialize_filter(_gen(code), "mp3 files", "gpt-4o-mini"))

    out = subprocess.run(  # noqa: S603
        [sys.executable, str(script), str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip().endswith("a.mp3")
    assert "b.txt" not in out.stdout


def test_saved_filter_standalone_harness_handles_file_and_multiple_roots(tmp_path):
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.py").write_text("x")
    (tmp_path / "c.txt").write_text("x")
    code = 'def filter_paths(paths):\n    return [p for p in paths if p.endswith(".py")]'
    script = tmp_path / "py.py"
    script.write_text(MODULE.serialize_filter(_gen(code), "py files", "gpt-4o-mini"))

    # A single file root: the file is enumerated (os.walk on a file would yield nothing).
    single = subprocess.run(  # noqa: S603
        [sys.executable, str(script), str(tmp_path / "a.py")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert single.stdout.strip() == str(tmp_path / "a.py")

    # Several file roots passed at once are all enumerated.
    multi = subprocess.run(  # noqa: S603
        [sys.executable, str(script), str(tmp_path / "a.py"), str(tmp_path / "b.py")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert set(multi.stdout.splitlines()) == {
        str(tmp_path / "a.py"),
        str(tmp_path / "b.py"),
    }


def test_harness_ignore_set_matches_default_ignores():
    # The standalone runner hardcodes the ignore set (it can't import nfind); guard drift.
    from nfind import _filter_harness
    from nfind.constants import DEFAULT_IGNORES

    assert set(DEFAULT_IGNORES) == _filter_harness._IGNORE


def test_saved_filter_standalone_harness_json_and_verbose(tmp_path):
    # Keep the script out of the searched tree so it doesn't match itself.
    data = tmp_path / "data"
    data.mkdir()
    (data / "a.py").write_text("x")
    code = (
        "def filter_paths(paths):\n"
        '    return [{"path": p, "n": 1} for p in paths if p.endswith(".py")]'
    )
    script = tmp_path / "py.py"
    script.write_text(MODULE.serialize_filter(_gen(code), "py files", "gpt-4o-mini"))

    def _run(*flags):
        return subprocess.run(  # noqa: S603
            [sys.executable, str(script), str(data), *flags],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    # Default: bare path. --verbose: path + extras. --json: count + records with extras.
    assert _run().strip() == str(data / "a.py")
    assert _run("--verbose").strip() == f"{data / 'a.py'}\tn=1"
    payload = json.loads(_run("--json"))
    assert payload == {"count": 1, "results": [{"path": str(data / "a.py"), "n": 1}]}


def test_saved_filter_standalone_harness_prunes_default_ignores(tmp_path):
    (tmp_path / "keep.py").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "hook.py").write_text("x")
    code = 'def filter_paths(paths):\n    return [p for p in paths if p.endswith(".py")]'
    script = tmp_path / "py.py"
    script.write_text(MODULE.serialize_filter(_gen(code), "py files", "gpt-4o-mini"))

    out = subprocess.run(  # noqa: S603
        [sys.executable, str(script), str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    # Mirrors nfind's default enumeration: .git is pruned, like `nfind` without --no-ignore.
    assert str(tmp_path / "keep.py") in out.stdout
    assert ".git" not in out.stdout


def test_deserialize_filter_round_trips_python_dependencies():
    src = MODULE.serialize_filter(
        _gen("def filter_paths(paths): return paths", ["mutagen"]), "mp3", "gpt-4o-mini"
    )
    parsed = MODULE.deserialize_filter(src, filename="mp3.py")
    assert parsed.runtime == "python"
    assert parsed.dependencies == ["mutagen"]


def test_deserialize_filter_detects_node_by_extension():
    src = MODULE.serialize_filter(
        _gen_node("function filterPaths(paths){return paths;}"), "ts", "gpt-4o-mini"
    )
    parsed = MODULE.deserialize_filter(src, filename="filter.cjs")
    assert parsed.runtime == "node"


def test_deserialize_filter_round_trips_node_dependencies():
    src = MODULE.serialize_filter(
        _gen_node("function filterPaths(paths){return paths;}", ["ts-morph", "@babel/parser"]),
        "ts",
        "gpt-4o-mini",
    )
    parsed = MODULE.deserialize_filter(src, filename="filter.cjs")
    assert parsed.runtime == "node"
    assert parsed.dependencies == ["@babel/parser", "ts-morph"]


def test_deserialize_filter_accepts_legacy_node_without_metadata():
    src = "// nfind filter\n// Runtime: node\n\nfunction filterPaths(paths){return paths;}\n"
    parsed = MODULE.deserialize_filter(src, filename="filter.cjs")
    assert parsed.runtime == "node"
    assert parsed.dependencies == []


def test_deserialize_filter_rejects_invalid_node_metadata():
    src = (
        '// nfind-metadata: {"runtime":"node","dependencies":["not a package"]}\n'
        "function filterPaths(paths){return paths;}\n"
    )
    with pytest.raises(ValueError, match="Invalid package name"):
        MODULE.deserialize_filter(src, filename="filter.cjs")


def test_deserialize_filter_rejects_invalid_python_dependencies():
    # A crafted saved file must not smuggle pip arguments through the PEP 723 block into
    # the image-build `pip install` line; validation rejects non-package-name strings.
    src = (
        "# /// script\n"
        '# requires-python = ">=3.12"\n'
        '# dependencies = ["requests==2.0 --extra-index-url http://evil.test"]\n'
        "# ///\n"
        '"""x"""\n\n\n'
        "def filter_paths(paths):\n    return paths\n"
    )
    with pytest.raises(ValueError, match="Invalid package name"):
        MODULE.deserialize_filter(src, filename="evil.py")


def test_run_saved_replays_without_generating(tmp_path):
    (tmp_path / "a.mp3").write_text("x")
    code = 'def filter_paths(paths):\n    return [p for p in paths if p.endswith(".mp3")]'
    script = tmp_path / "mp3.py"
    script.write_text(MODULE.serialize_filter(_gen(code, ["mutagen"]), "mp3", "gpt-4o-mini"))

    container = "/data/a.mp3"
    with (
        patch.object(MODULE, "check_docker_available"),
        patch.object(EXECUTION, "build_worker_image", return_value="img:deps"),
        patch.object(MODULE, "generate_filter") as generate,
        patch.object(EXECUTION, "run_filter", return_value=[{"path": container}]) as run_filter,
        patch.object(
            MODULE,
            "enumerate_paths",
            return_value=([container], {container: str(tmp_path / "a.mp3")}),
        ),
    ):
        records = MODULE.run_saved(script, str(tmp_path))

    generate.assert_not_called()
    run_filter.assert_called_once()
    assert records == [{"path": str(tmp_path / "a.mp3")}]


def test_run_saved_gates_unapproved_dependencies(tmp_path):
    code = "def filter_paths(paths): return paths"
    script = tmp_path / "f.py"
    script.write_text(MODULE.serialize_filter(_gen(code, ["sketchy-pkg"]), "x", "gpt-4o-mini"))

    container = "/data/a"
    with (
        patch.object(MODULE, "check_docker_available"),
        patch.object(EXECUTION, "build_worker_image") as build,
        patch.object(EXECUTION, "run_filter") as run_filter,
        patch.object(MODULE, "enumerate_paths", return_value=([container], {container: "a"})),
        pytest.raises(MODULE.DependencyError, match="sketchy-pkg"),
    ):
        MODULE.run_saved(script, str(tmp_path), whitelist=set())

    build.assert_not_called()
    run_filter.assert_not_called()


def test_run_saved_gates_unapproved_node_dependencies(tmp_path):
    code = "function filterPaths(paths){ return paths; }"
    script = tmp_path / "f.cjs"
    script.write_text(MODULE.serialize_filter(_gen_node(code, ["left-pad"]), "x", "gpt-4o-mini"))

    container = "/data/a"
    with (
        patch.object(MODULE, "check_docker_available"),
        patch.object(EXECUTION, "build_worker_image") as build,
        patch.object(EXECUTION, "run_filter") as run_filter,
        patch.object(MODULE, "enumerate_paths", return_value=([container], {container: "a"})),
        pytest.raises(MODULE.DependencyError, match="left-pad"),
    ):
        MODULE.run_saved(script, str(tmp_path), whitelist=set())

    build.assert_not_called()
    run_filter.assert_not_called()


def test_cli_save_writes_replayable_script(tmp_path):
    from typer.testing import CliRunner

    (tmp_path / "file.txt").write_text("content")
    out = tmp_path / "saved.py"
    runner = CliRunner()
    with (
        patch.object(cli.backend, "check_docker_available"),
        patch.object(EXECUTION, "build_worker_image", return_value=EXECUTION.DEFAULT_IMAGE),
        patch.object(
            cli.backend, "generate_filter", return_value=_gen("def filter_paths(paths): return []")
        ),
        patch.object(EXECUTION, "run_filter", return_value=[]),
    ):
        result = runner.invoke(cli.app, ["files", str(tmp_path), "--save", str(out)])

    assert result.exit_code == 0
    written = out.read_text()
    assert "# /// script" in written
    assert "Prompt:  files" in written


def test_cli_show_code_renders_full_saved_script(tmp_path):
    from typer.testing import CliRunner

    (tmp_path / "file.txt").write_text("content")
    runner = CliRunner()
    with (
        patch.object(cli.backend, "check_docker_available"),
        patch.object(EXECUTION, "build_worker_image", return_value=EXECUTION.DEFAULT_IMAGE),
        patch.object(
            cli.backend,
            "generate_filter",
            return_value=_gen("def filter_paths(paths): return []"),
        ),
        patch.object(EXECUTION, "run_filter", return_value=[]),
    ):
        result = runner.invoke(cli.app, ["epub files", str(tmp_path), "--show-code"])

    assert result.exit_code == 0
    # --show-code previews the full script --save would write, not just the function.
    assert "# /// script" in result.output
    assert "Prompt:  epub files" in result.output
    assert "def filter_paths(paths):" in result.output


def test_cli_run_uses_single_positional_as_path(tmp_path):
    from typer.testing import CliRunner

    runner = CliRunner()
    script = tmp_path / "f.py"
    script.write_text("def filter_paths(paths): return paths")
    target = tmp_path / "sub"
    target.mkdir()

    with patch.object(cli.backend, "run_saved", return_value=[]) as run_saved:
        result = runner.invoke(cli.app, ["--run", str(script), str(target)])

    assert result.exit_code == 0
    # The lone positional is the search PATH (not a PROMPT) when --run is used.
    assert run_saved.call_args.args[1] == [str(target)]


def test_cli_run_accepts_multiple_positionals_as_paths(tmp_path):
    from typer.testing import CliRunner

    runner = CliRunner()
    script = tmp_path / "f.py"
    script.write_text("def filter_paths(paths): return paths")
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()

    with patch.object(cli.backend, "run_saved", return_value=[]) as run_saved:
        result = runner.invoke(cli.app, ["--run", str(script), str(first), str(second)])

    assert result.exit_code == 0
    # Every positional is a search PATH under --run; none is taken as a PROMPT.
    assert run_saved.call_args.args[1] == [str(first), str(second)]


def test_cli_run_rejects_save(tmp_path):
    from typer.testing import CliRunner

    runner = CliRunner()
    script = tmp_path / "f.py"
    script.write_text("def filter_paths(paths): return paths")

    with_save = runner.invoke(cli.app, ["--run", str(script), "--save", str(tmp_path / "o.py")])
    assert with_save.exit_code == 2


def test_cli_shows_help_with_no_args():
    from typer.testing import CliRunner

    result = CliRunner().invoke(cli.app, [])
    assert result.exit_code == 2
    assert "Usage:" in result.output


def test_cli_requires_prompt_without_run():
    from typer.testing import CliRunner

    # Options but no PROMPT and no --run is a usage error (not bare no-args help).
    result = CliRunner().invoke(cli.app, ["--json"])
    assert result.exit_code == 2
    assert "PROMPT is required" in result.output
