"""Pure command planning helpers for the CLI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ListModelsRequest:
    model: str


@dataclass(frozen=True)
class GeneratedSearchRequest:
    prompt: str
    paths: list[str]


@dataclass(frozen=True)
class SavedReplayRequest:
    filter_path: Path
    paths: list[str]


CommandRequest = GeneratedSearchRequest | SavedReplayRequest | ListModelsRequest


def normalize_search_paths(paths: list[str] | None) -> list[str]:
    return paths if paths else ["."]


def validate_output_modes(*, as_json: bool, verbose: bool, print0: bool) -> None:
    if as_json and verbose:
        raise ValueError("--json and --verbose are mutually exclusive.")
    if print0 and (as_json or verbose):
        raise ValueError("--print0 cannot be combined with --json or --verbose.")


def validate_dependency_modes(*, yes: bool, no_deps: bool) -> None:
    if yes and no_deps:
        raise ValueError("--yes and --no-deps are mutually exclusive.")


def validate_max_depth(max_depth: int | None) -> None:
    if max_depth is not None and max_depth < 1:
        raise ValueError("--max-depth must be at least 1.")


def build_command_request(
    *,
    prompt: str | None,
    paths: list[str] | None,
    list_models: bool,
    model: str,
    run: Path | None,
    save: Path | None,
    confirm: bool,
    macos_meta: bool,
) -> CommandRequest:
    if list_models:
        return ListModelsRequest(model=model)
    if run is not None:
        run_paths = [prompt, *(paths or [])] if prompt is not None else paths
        if save is not None:
            raise ValueError("--save cannot be combined with --run.")
        if confirm:
            raise ValueError("--confirm cannot be combined with --run.")
        if macos_meta:
            raise ValueError("--macos-meta cannot be combined with --run.")
        return SavedReplayRequest(filter_path=run, paths=normalize_search_paths(run_paths))
    if prompt is None:
        raise ValueError("PROMPT is required (or use --run to replay a saved filter).")
    return GeneratedSearchRequest(prompt=prompt, paths=normalize_search_paths(paths))


def plan_command(
    *,
    prompt: str | None,
    paths: list[str] | None,
    list_models: bool,
    model: str,
    run: Path | None,
    save: Path | None,
    confirm: bool,
    macos_meta: bool,
    as_json: bool,
    verbose: bool,
    print0: bool,
    yes: bool,
    no_deps: bool,
    max_depth: int | None,
) -> CommandRequest:
    validate_output_modes(as_json=as_json, verbose=verbose, print0=print0)
    validate_dependency_modes(yes=yes, no_deps=no_deps)
    validate_max_depth(max_depth)
    return build_command_request(
        prompt=prompt,
        paths=paths,
        list_models=list_models,
        model=model,
        run=run,
        save=save,
        confirm=confirm,
        macos_meta=macos_meta,
    )
