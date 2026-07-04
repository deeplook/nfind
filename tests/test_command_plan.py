from pathlib import Path

import pytest

from nfind.command_plan import (
    GeneratedSearchRequest,
    ListModelsRequest,
    SavedReplayRequest,
    plan_command,
)


def _plan(**overrides):
    options = {
        "prompt": "files",
        "paths": ["/tmp"],
        "list_models": False,
        "model": "gpt-4o-mini",
        "run": None,
        "save": None,
        "confirm": False,
        "macos_meta": False,
        "as_json": False,
        "fields": False,
        "print0": False,
        "extract": False,
        "extract_field": None,
        "yes": False,
        "no_deps": False,
        "max_depth": None,
    }
    options.update(overrides)
    # The options dict is deliberately heterogeneous; a **splat of it can't be
    # matched to plan_command's typed parameters statically.
    return plan_command(**options)  # type: ignore[arg-type]


def test_plans_generated_search_request():
    request = _plan(prompt="audio", paths=["/music", "/archive"])

    assert request == GeneratedSearchRequest(prompt="audio", paths=["/music", "/archive"])


def test_generated_search_with_no_paths_has_empty_paths():
    request = _plan(paths=None)

    assert request == GeneratedSearchRequest(prompt="files", paths=[])


def test_plans_list_models_request_without_prompt():
    request = _plan(prompt=None, paths=None, list_models=True, model="groq/llama-3.3")

    assert request == ListModelsRequest(model="groq/llama-3.3")


def test_run_folds_prompt_positionals_into_paths(tmp_path):
    script = tmp_path / "filter.py"
    request = _plan(prompt="/first", paths=["/second"], run=script)

    assert request == SavedReplayRequest(filter_path=script, paths=["/first", "/second"])


def test_run_defaults_to_empty_paths(tmp_path):
    script = tmp_path / "filter.py"
    request = _plan(prompt=None, paths=None, run=script)

    assert request == SavedReplayRequest(filter_path=script, paths=[])


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"prompt": None}, "PROMPT is required"),
        ({"as_json": True, "fields": True}, "--json and --fields"),
        ({"print0": True, "as_json": True}, "--print0 cannot be combined"),
        ({"print0": True, "fields": True}, "--print0 cannot be combined"),
        ({"extract": True, "fields": True}, "--extract and --fields"),
        ({"extract_field": "todos"}, "--extract-field requires --extract"),
        ({"yes": True, "no_deps": True}, "--yes and --no-deps"),
        ({"max_depth": 0}, "--max-depth"),
        ({"command_timeout": 0}, "--command-timeout"),
        ({"max_results": -1}, "--max-results"),
        ({"max_items": 0}, "--max-items"),
        ({"max_output_bytes": 0}, "--max-output-bytes"),
    ],
)
def test_rejects_invalid_command_options(overrides, message):
    with pytest.raises(ValueError, match=message):
        _plan(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"extract": True},
        {"extract": True, "print0": True},
        {"extract": True, "as_json": True},
        {"extract": True, "extract_field": "todos"},
    ],
)
def test_accepts_extract_combinations(overrides):
    request = _plan(**overrides)

    assert isinstance(request, GeneratedSearchRequest)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"save": Path("out.py")}, "--save cannot be combined"),
        ({"confirm": True}, "--confirm cannot be combined"),
        ({"macos_meta": True}, "--macos-meta cannot be combined"),
    ],
)
def test_run_rejects_incompatible_options(tmp_path, overrides, message):
    with pytest.raises(ValueError, match=message):
        _plan(run=tmp_path / "filter.py", **overrides)
