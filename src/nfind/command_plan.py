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
    return paths or []


def validate_output_modes(*, as_json: bool, fields: bool, print0: bool, extract: bool) -> None:
    if as_json and fields:
        raise ValueError("--json and --fields are mutually exclusive.")
    if print0 and (as_json or fields):
        raise ValueError("--print0 cannot be combined with --json or --fields.")
    if extract and fields:
        raise ValueError("--extract and --fields are mutually exclusive.")


def validate_extract_modes(*, extract: bool, extract_field: str | None) -> None:
    if extract_field is not None and not extract:
        raise ValueError("--extract-field requires --extract.")


def validate_dependency_modes(*, yes: bool, no_deps: bool) -> None:
    if yes and no_deps:
        raise ValueError("--yes and --no-deps are mutually exclusive.")


def validate_max_depth(max_depth: int | None) -> None:
    if max_depth is not None and max_depth < 1:
        raise ValueError("--max-depth must be at least 1.")


def validate_limits(**limits: int | float | None) -> None:
    for name, value in limits.items():
        if value is not None and value <= 0:
            raise ValueError(f"--{name.replace('_', '-')} must be positive.")


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
    fields: bool,
    print0: bool,
    extract: bool,
    extract_field: str | None,
    yes: bool,
    no_deps: bool,
    max_depth: int | None,
    command_timeout: float | None = None,
    max_results: int | None = None,
    max_items: int | None = None,
    max_output_bytes: int | None = None,
) -> CommandRequest:
    validate_output_modes(as_json=as_json, fields=fields, print0=print0, extract=extract)
    validate_extract_modes(extract=extract, extract_field=extract_field)
    validate_dependency_modes(yes=yes, no_deps=no_deps)
    validate_max_depth(max_depth)
    validate_limits(
        command_timeout=command_timeout,
        max_results=max_results,
        max_items=max_items,
        max_output_bytes=max_output_bytes,
    )
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
