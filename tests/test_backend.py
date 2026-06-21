import json
import plistlib
import subprocess
import sys
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


def _fake_openai(*contents):
    """Patch the generation client to return the given reply contents in order.

    Patches ``_make_client`` so the tests exercise the generate/retry logic without
    needing real provider credentials.
    """
    responses = [Mock(choices=[Mock(message=Mock(content=content))]) for content in contents]
    client = Mock()
    client.chat.completions.create.side_effect = responses
    return patch.object(MODULE, "_make_client", return_value=client), client


def test_split_model_defaults_to_openai_for_bare_name():
    assert MODULE._split_model("gpt-4o-mini") == ("openai", "gpt-4o-mini")


def test_split_model_parses_provider_prefix():
    assert MODULE._split_model("anthropic/claude-3-5-sonnet") == (
        "anthropic",
        "claude-3-5-sonnet",
    )
    # Only the first slash splits; vendor-qualified names pass through.
    assert MODULE._split_model("openrouter/anthropic/claude-3") == (
        "openrouter",
        "anthropic/claude-3",
    )
    # Stray whitespace and an empty half fall back to the default provider.
    assert MODULE._split_model("  groq/llama-3.3  ") == ("groq", "llama-3.3")
    assert MODULE._split_model("/oops") == ("openai", "/oops")


def test_make_client_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unknown model provider"):
        MODULE._make_client("nope")


def test_make_client_missing_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        MODULE._make_client("anthropic")


def test_make_client_uses_base_url_and_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "secret")
    with patch("openai.OpenAI") as ctor:
        MODULE._make_client("groq")
    assert ctor.call_args.kwargs == {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "secret",
    }


def test_make_client_local_provider_needs_no_key(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    with patch("openai.OpenAI") as ctor:
        MODULE._make_client("ollama")
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
    assert json.loads(MODULE._extract_json_object(content)) == {"code": "x"}


def test_generate_filter_drops_json_mode_when_provider_rejects_it():
    good = json.dumps({"code": "def filter_paths(paths): return paths"})
    client = Mock()
    # First call (with response_format) errors; the retry without it succeeds.
    client.chat.completions.create.side_effect = [
        Exception("response_format not supported"),
        Mock(choices=[Mock(message=Mock(content=good))]),
    ]
    with patch.object(MODULE, "_make_client", return_value=client):
        result = MODULE.generate_filter("anything", model="groq/llama-3.3")
    assert result.code == "def filter_paths(paths): return paths"
    assert client.chat.completions.create.call_count == 2
    first, second = client.chat.completions.create.call_args_list
    assert "response_format" in first.kwargs
    assert "response_format" not in second.kwargs
    assert first.kwargs["model"] == "llama-3.3"  # provider prefix stripped


def test_generate_filter_succeeds_on_first_attempt():
    good = json.dumps({"code": "def filter_paths(paths): return paths"})
    patcher, client = _fake_openai(good)
    with patcher:
        result = MODULE.generate_filter("anything")
    assert result.code == "def filter_paths(paths): return paths"
    assert client.chat.completions.create.call_count == 1


def test_generate_filter_retries_on_invalid_then_succeeds():
    good = json.dumps({"code": "def filter_paths(paths): return paths"})
    patcher, client = _fake_openai("not json", good)
    retries = []
    with patcher:
        result = MODULE.generate_filter("anything", on_retry=lambda n, exc: retries.append(n))
    assert result.code == "def filter_paths(paths): return paths"
    assert client.chat.completions.create.call_count == 2
    assert retries == [1]
    # The corrective message is fed back before retrying.
    second_call = client.chat.completions.create.call_args_list[1]
    messages = second_call.kwargs["messages"]
    assert messages[-2]["role"] == "assistant" and messages[-2]["content"] == "not json"
    assert messages[-1]["role"] == "user"
    # Retries leave temperature 0 behind so the model diverges.
    assert second_call.kwargs["temperature"] == MODULE._RETRY_TEMPERATURE


def test_generate_filter_raises_after_exhausting_attempts():
    patcher, client = _fake_openai("not json", "still not json")
    with patcher, pytest.raises(ValueError, match="after 2 attempt"):
        MODULE.generate_filter("anything", attempts=2)
    assert client.chat.completions.create.call_count == 2


def test_generate_filter_rejects_nonpositive_attempts():
    with pytest.raises(ValueError, match="attempts must be at least 1"):
        MODULE.generate_filter("anything", attempts=0)


def test_generate_filter_appends_macos_meta_guidance_only_when_enabled():
    good = json.dumps({"code": "def filter_paths(paths): return paths"})

    patcher, client = _fake_openai(good)
    with patcher:
        MODULE.generate_filter("anything", macos_meta=True)
    system = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert "META" in system and "quarantined" in system

    patcher, client = _fake_openai(good)
    with patcher:
        MODULE.generate_filter("anything")
    system = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert "META" not in system


def test_collect_macos_metadata_empty_off_darwin(monkeypatch):
    monkeypatch.setattr(MODULE.sys, "platform", "linux")
    assert MODULE.collect_macos_metadata({"/data/a": "/host/a"}) == {}


@pytest.mark.skipif(sys.platform != "darwin", reason="reads macOS extended attributes")
def test_collect_macos_metadata_reads_tags_and_quarantine(tmp_path):
    libc = MODULE.ctypes.CDLL(MODULE.ctypes.util.find_library("c"), use_errno=True)
    libc.setxattr.argtypes = [
        MODULE.ctypes.c_char_p,
        MODULE.ctypes.c_char_p,
        MODULE.ctypes.c_void_p,
        MODULE.ctypes.c_size_t,
        MODULE.ctypes.c_uint32,
        MODULE.ctypes.c_int,
    ]

    def setxattr(path, name, value):
        rc = libc.setxattr(str(path).encode(), name.encode(), value, len(value), 0, 0)
        assert rc == 0, MODULE.ctypes.get_errno()

    tagged = tmp_path / "tagged.txt"
    tagged.write_text("x")
    setxattr(
        tagged, MODULE._XATTR_TAGS, plistlib.dumps(["Red\n6", "Work"], fmt=plistlib.FMT_BINARY)
    )
    setxattr(tagged, MODULE._XATTR_QUARANTINE, b"0083;0;Safari;")
    setxattr(
        tagged,
        MODULE._XATTR_WHERE_FROMS,
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


def test_derived_image_tag_is_stable_and_order_independent():
    tag1 = MODULE._derived_image_tag("pfind-search-paths:latest", ["a", "b"])
    tag2 = MODULE._derived_image_tag("pfind-search-paths:latest", ["b", "a"])
    assert tag1 == tag2
    assert tag1.startswith("pfind-search-paths:deps-")


def test_whitelist_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("PFIND_WHITELIST", str(tmp_path / "whitelist.json"))
    assert "rarfile" not in MODULE.load_whitelist()
    assert "mutagen" in MODULE.load_whitelist()  # built-in default
    assert "tree-sitter-python" in MODULE.load_whitelist()  # multi-language parsing
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
    written = out.read_text()
    assert "def filter_paths(paths): return []" in written
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


# --- saved filters: render / parse / replay -------------------------------------


def test_render_saved_filter_python_is_valid_pep723_script():
    code = "def filter_paths(paths):\n    import mutagen\n    return paths"
    src = MODULE.render_saved_filter(_gen(code, ["mutagen"]), "MP3 files", "gpt-4o-mini")

    # Valid Python and a parseable PEP 723 block declaring the dependency.
    compile(src, "saved.py", "exec")
    match = MODULE._PEP723_RE.search(src)
    assert match is not None
    assert '"mutagen"' in match.group("body")
    # Docstring carries the prompt and the safety warning; code is included verbatim.
    assert "Prompt:  MP3 files" in src
    assert "OUTSIDE the pfind Docker sandbox" in src
    assert "def filter_paths(paths):" in src
    assert 'if __name__ == "__main__":' in src


def test_render_saved_filter_escapes_triple_quotes_in_prompt():
    src = MODULE.render_saved_filter(
        _gen("def filter_paths(paths): return paths"), 'a """ quote', "gpt-4o-mini"
    )
    compile(src, "saved.py", "exec")


def test_render_saved_filter_node_has_comment_header_and_raw_code():
    code = "function filterPaths(paths){ return paths; }"
    src = MODULE.render_saved_filter(_gen_node(code, ["ts-morph"]), "TS files", "gpt-4o-mini")

    assert src.startswith("// pfind filter")
    assert "// Prompt:  TS files" in src
    assert "python-only" in src
    assert code in src
    assert "# /// script" not in src  # no PEP 723 block for node


def test_saved_filter_standalone_harness_runs(tmp_path):
    (tmp_path / "a.mp3").write_text("x")
    (tmp_path / "b.txt").write_text("x")
    code = 'def filter_paths(paths):\n    return [p for p in paths if p.endswith(".mp3")]'
    script = tmp_path / "mp3.py"
    script.write_text(MODULE.render_saved_filter(_gen(code), "mp3 files", "gpt-4o-mini"))

    out = subprocess.run(  # noqa: S603
        [sys.executable, str(script), str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip().endswith("a.mp3")
    assert "b.txt" not in out.stdout


def test_parse_saved_filter_round_trips_python_dependencies():
    src = MODULE.render_saved_filter(
        _gen("def filter_paths(paths): return paths", ["mutagen"]), "mp3", "gpt-4o-mini"
    )
    parsed = MODULE.parse_saved_filter(src, filename="mp3.py")
    assert parsed.runtime == "python"
    assert parsed.dependencies == ["mutagen"]


def test_parse_saved_filter_detects_node_by_extension():
    src = MODULE.render_saved_filter(
        _gen_node("function filterPaths(paths){return paths;}"), "ts", "gpt-4o-mini"
    )
    parsed = MODULE.parse_saved_filter(src, filename="filter.cjs")
    assert parsed.runtime == "node"


def test_run_saved_replays_without_generating(tmp_path):
    (tmp_path / "a.mp3").write_text("x")
    code = 'def filter_paths(paths):\n    return [p for p in paths if p.endswith(".mp3")]'
    script = tmp_path / "mp3.py"
    script.write_text(MODULE.render_saved_filter(_gen(code, ["mutagen"]), "mp3", "gpt-4o-mini"))

    container = "/data/a.mp3"
    with (
        patch.object(MODULE, "check_docker_available"),
        patch.object(MODULE, "build_worker_image", return_value="img:deps"),
        patch.object(MODULE, "generate_filter") as generate,
        patch.object(MODULE, "run_filter", return_value=[{"path": container}]) as run_filter,
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
    script.write_text(MODULE.render_saved_filter(_gen(code, ["sketchy-pkg"]), "x", "gpt-4o-mini"))

    container = "/data/a"
    with (
        patch.object(MODULE, "check_docker_available"),
        patch.object(MODULE, "build_worker_image") as build,
        patch.object(MODULE, "run_filter") as run_filter,
        patch.object(MODULE, "enumerate_paths", return_value=([container], {container: "a"})),
        pytest.raises(MODULE.DependencyError, match="sketchy-pkg"),
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
        patch.object(cli.backend, "build_worker_image", return_value=cli.backend.DEFAULT_IMAGE),
        patch.object(
            cli.backend, "generate_filter", return_value=_gen("def filter_paths(paths): return []")
        ),
        patch.object(cli.backend, "run_filter", return_value=[]),
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
        patch.object(cli.backend, "build_worker_image", return_value=cli.backend.DEFAULT_IMAGE),
        patch.object(
            cli.backend,
            "generate_filter",
            return_value=_gen("def filter_paths(paths): return []"),
        ),
        patch.object(cli.backend, "run_filter", return_value=[]),
    ):
        result = runner.invoke(cli.app, ["epub files", str(tmp_path), "--show-code"])

    assert result.exit_code == 0
    # --show-code previews the full script --save would write, not just the function.
    assert "# /// script" in result.output
    assert "Prompt:  epub files" in result.output
    assert "def filter_paths(paths): return []" in result.output


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
    assert run_saved.call_args.args[1] == str(target)


def test_cli_run_rejects_extra_positional_and_save(tmp_path):
    from typer.testing import CliRunner

    runner = CliRunner()
    script = tmp_path / "f.py"
    script.write_text("def filter_paths(paths): return paths")

    two_positionals = runner.invoke(cli.app, ["prompt", "path", "--run", str(script)])
    assert two_positionals.exit_code == 2

    with_save = runner.invoke(cli.app, ["--run", str(script), "--save", str(tmp_path / "o.py")])
    assert with_save.exit_code == 2


def test_cli_requires_prompt_without_run():
    from typer.testing import CliRunner

    result = CliRunner().invoke(cli.app, [])
    assert result.exit_code == 2
