"""Command-line interface for nfind."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any, cast

import typer

from . import backend
from . import sandbox as sandbox_module
from .backend import (
    DEFAULT_BUILD_TIMEOUT,
    DEFAULT_MODEL,
    DEFAULT_SANDBOX_BACKEND,
    DockerError,
    GeneratedFilter,
    SandboxBackend,
)
from .command_plan import (
    CommandRequest,
    GeneratedSearchRequest,
    ListModelsRequest,
    SavedReplayRequest,
    plan_command,
)
from .config import ConfigError, default_config_path, load_config
from .constants import (
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_CPUS,
    DEFAULT_MEMORY,
    DEFAULT_PIDS_LIMIT,
    DEFAULT_TIMEOUT,
)
from .deadline import arm_command_timeout
from .extract import iter_extract_rows

app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Find files by describing them in natural language.",
)

_APPLE_SANDBOX_MACOS15_WARNING = (
    "warning: Apple Containers sandbox is experimental and does not disable networking "
    "on macOS 15; nfind uses --no-dns, but raw IP network access may still be possible. "
    "Apple Containers also lacks Docker-equivalent pids-limit and no-new-privileges "
    "flags in the current CLI. Use Docker for the strongest sandbox."
)

_APPLE_SANDBOX_MACOS26_WARNING = (
    "warning: Apple Containers sandbox is experimental; on macOS 26+ nfind uses "
    "--network none for network isolation, but Apple Containers still lacks "
    "Docker-equivalent pids-limit and no-new-privileges flags in the current CLI. "
    "Use Docker for the strongest sandbox."
)


_PODMAN_SANDBOX_WARNING = (
    "warning: Podman sandbox is experimental; nfind applies the same hardening flags as "
    "Docker, but the backend has not been validated against a real Podman runtime. Use "
    "Docker for the most thoroughly tested sandbox."
)


def _validate_sandbox_backend(value: str) -> SandboxBackend:
    if value in sandbox_module.SANDBOX_BACKENDS:
        return cast(SandboxBackend, value)
    choices = ", ".join(sandbox_module.SANDBOX_BACKENDS)
    raise ValueError(f"--sandbox must be one of: {choices}")


def _warn_if_experimental_sandbox(sandbox_backend: SandboxBackend) -> None:
    if sandbox_backend == "apple":
        warning = (
            _APPLE_SANDBOX_MACOS26_WARNING
            if sandbox_module.apple_supports_no_network_flag()
            else _APPLE_SANDBOX_MACOS15_WARNING
        )
        typer.echo(warning, err=True)
    elif sandbox_backend == "podman":
        typer.echo(_PODMAN_SANDBOX_WARNING, err=True)


def _highlight(code: str, runtime: str = "python") -> str:
    """Syntax-highlight generated source for the terminal.

    Picks a lexer for the filter's runtime (Python or Node.js/JavaScript). Honors
    the NO_COLOR convention and falls back to plain text when stderr is not a TTY
    (so redirected or piped output stays clean) or Pygments is absent.
    """
    if "NO_COLOR" in os.environ or not sys.stderr.isatty():
        return code
    try:
        from pygments import highlight
        from pygments.formatters import TerminalFormatter
        from pygments.lexers import JavascriptLexer, PythonLexer
    except ImportError:
        return code
    lexer = JavascriptLexer() if runtime == "node" else PythonLexer()
    highlighted: str = highlight(code, lexer, TerminalFormatter())
    return highlighted.rstrip("\n")


def _load_config_defaults(ctx: typer.Context, value: Path | None) -> Path | None:
    """Populate Click's ``default_map`` from a TOML config file before other options parse.

    Runs as an eager-option callback so the file's values become the defaults for the
    remaining options, with command-line arguments still taking precedence. An explicit
    ``--config``/``NFIND_CONFIG`` path must exist; the default location is used only when
    present.
    """
    if value is not None:
        path = value.expanduser()
        if not path.is_file():
            raise typer.BadParameter(f"config file not found: {path}")
    else:
        path = default_config_path()
        if not path.is_file():
            return value
    try:
        defaults = load_config(path)
    except ConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc
    ctx.default_map = {**(ctx.default_map or {}), **defaults}
    return value


def _emit(
    records: list[dict[str, Any]],
    *,
    as_json: bool,
    fields: bool,
    print0: bool,
    extract: bool = False,
    extract_field: str | None = None,
    max_results: int | None = None,
    max_items: int | None = None,
    max_output_bytes: int | None = None,
) -> None:
    """Render result records in the requested output mode.

    Default: one path per line. ``--print0``: paths separated by NUL bytes (for
    ``xargs -0``). ``--json``: a JSON object with count and the full records (path plus
    any extra fields). ``--fields``: each path followed by its extra fields, when the
    filter produced any; a list-valued field is summarised as its element count
    (``todos=3``) rather than dumped, since ``key=value`` cannot faithfully render a
    nested object -- use ``--extract`` or ``--json`` for the elements. ``--extract``:
    explode each record's list-valued field into one
    ``path[:line]<TAB><payload>`` line per element (NUL-separated under ``--print0``);
    ``--json`` always wins and stays nested, so ``--extract`` only affects text output.
    """
    truncated_by: list[str] = []
    limited = records
    if max_results is not None and len(limited) > max_results:
        limited = limited[:max_results]
        truncated_by.append("max-results")

    if as_json:

        def encode_json(current: list[dict[str, Any]]) -> str:
            payload: dict[str, Any] = {"count": len(current), "results": current}
            if truncated_by:
                payload.update(truncated=True, truncated_by=truncated_by)
            return json.dumps(payload, indent=2)

        output = encode_json(limited)
        if max_output_bytes is not None:
            while len(output.encode("utf-8", "surrogateescape")) + 1 > max_output_bytes:
                if "max-output-bytes" not in truncated_by:
                    truncated_by.append("max-output-bytes")
                if not limited:
                    raise ValueError("--max-output-bytes is too small for a valid JSON result.")
                limited = limited[:-1]
                output = encode_json(limited)
        typer.echo(output)
    else:
        if extract:
            rows = iter_extract_rows(limited, extract_field)
        else:

            def rendered_rows() -> Iterator[str]:
                for record in limited:
                    path = record["path"]
                    extras = {key: value for key, value in record.items() if key != "path"}
                    if fields and extras:
                        detail = ", ".join(
                            f"{key}={len(value)}" if isinstance(value, list) else f"{key}={value}"
                            for key, value in extras.items()
                        )
                        yield f"{path}\t{detail}"
                    else:
                        yield path

            rows = rendered_rows()

        separator = "\0" if print0 else "\n"
        written = 0
        for index, row in enumerate(rows):
            if extract and max_items is not None and index >= max_items:
                truncated_by.append("max-items")
                break
            encoded_size = len(f"{row}{separator}".encode("utf-8", "surrogateescape"))
            if max_output_bytes is not None and written + encoded_size > max_output_bytes:
                truncated_by.append("max-output-bytes")
                break
            sys.stdout.write(f"{row}{separator}")
            written += encoded_size

    if truncated_by:
        labels = ", ".join(dict.fromkeys(truncated_by))
        warning = f"warning: output truncated by {labels}; increase the limit to see more"
        typer.echo(warning, err=True)


def _read_stdin_paths() -> list[str]:
    """Read a path list from stdin, splitting on NUL when present, else on newlines.

    NUL auto-detection lets ``-`` consume both ``find -print0`` / ``nfind --print0``
    (NUL-delimited, safe for odd filenames) and plain newline-delimited lists without a
    separate flag. Empty entries (e.g. a trailing separator) are dropped.
    """
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        data = buffer.read()
        parts = data.split(b"\0") if b"\0" in data else data.splitlines()
        return [part.decode("utf-8", "surrogateescape") for part in parts if part]
    text = sys.stdin.read()
    text_parts = text.split("\0") if "\0" in text else text.splitlines()
    return [part for part in text_parts if part]


def _resolve_stdin_paths(request: CommandRequest) -> tuple[CommandRequest, bool]:
    """Expand a ``-`` path argument by reading the root list from stdin.

    Returns the (possibly rewritten) request and a flag that is true when stdin was
    requested but yielded no paths -- the caller should then emit nothing and exit rather
    than let an empty root list fall back to searching the current directory.
    """
    if not isinstance(request, (GeneratedSearchRequest, SavedReplayRequest)):
        return request, False
    if "-" not in request.paths:
        return request, False
    if sys.stdin.isatty():
        raise ValueError(
            "reading paths from stdin ('-') but stdin is a terminal; pipe a path list "
            'in, e.g. \'find . -name "*.py" | nfind "..." -\''
        )
    stdin_paths = _read_stdin_paths()
    expanded: list[str] = []
    for path in request.paths:
        if path == "-":
            expanded.extend(stdin_paths)
        else:
            expanded.append(path)
    return replace(request, paths=expanded), not expanded


@app.command(no_args_is_help=True)
def main(
    ctx: typer.Context,
    prompt: Annotated[
        str | None,
        typer.Argument(
            help="Natural-language description of the paths to find. "
            "Omit when replaying a saved filter with --run.",
        ),
    ] = None,
    paths: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="[PATH]...",
            help="One or more directories or files to search. Directories are walked "
            "recursively, with common ignored names pruned unless --no-ignore is set. "
            "With several, each is searched and results are merged. Use '-' to read a "
            "NUL- or newline-delimited path list from stdin (e.g. 'find . | nfind "
            '"..." -\'). If '
            "omitted, the filter is generated but not run (useful with --save or "
            "--show-code).",
        ),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config",
            envvar="NFIND_CONFIG",
            is_eager=True,
            callback=_load_config_defaults,
            help="TOML config file supplying reusable option defaults, including "
            "models, resource limits, output limits, and enumeration controls. "
            "Defaults to config.toml in nfind's per-user config directory; "
            "command-line options win.",
        ),
    ] = None,
    model: Annotated[
        str,
        typer.Option(
            help="Model used to generate the filter. Bare name uses OpenAI; use "
            "'provider/model' for others (e.g. anthropic/claude-opus-4-8, "
            "ollama/llama3.1, openrouter/<vendor>/<model>).",
        ),
    ] = DEFAULT_MODEL,
    list_models: Annotated[
        bool,
        typer.Option(
            "--list-models",
            help="List the model ids available for the selected provider (from --model) "
            "and exit. Needs that provider's API key set.",
        ),
    ] = False,
    image: Annotated[
        str | None,
        typer.Option(help="Override the base image tag for the chosen runtime."),
    ] = None,
    sandbox_backend: Annotated[
        str,
        typer.Option(
            "--sandbox",
            help="Sandbox backend: docker (default), apple (Apple Containers, experimental), "
            "or podman (experimental).",
        ),
    ] = DEFAULT_SANDBOX_BACKEND,
    timeout: Annotated[
        float,
        typer.Option(help="Seconds the generated filter may run before it is killed."),
    ] = DEFAULT_TIMEOUT,
    command_timeout: Annotated[
        float | None,
        typer.Option(
            "--command-timeout",
            help="Optional POSIX wall-clock deadline for the entire command, in seconds.",
        ),
    ] = DEFAULT_COMMAND_TIMEOUT,
    memory: Annotated[
        str,
        typer.Option(help="Memory limit for the worker container (e.g. 256m)."),
    ] = DEFAULT_MEMORY,
    cpus: Annotated[
        float,
        typer.Option(help="CPU limit for the worker container."),
    ] = DEFAULT_CPUS,
    pids_limit: Annotated[
        int,
        typer.Option(help="Maximum number of processes inside the worker container."),
    ] = DEFAULT_PIDS_LIMIT,
    rebuild: Annotated[
        bool,
        typer.Option(help="Rebuild the worker image before searching."),
    ] = False,
    build_timeout: Annotated[
        float,
        typer.Option(help="Seconds allowed for building the worker image."),
    ] = DEFAULT_BUILD_TIMEOUT,
    show_code: Annotated[
        bool,
        typer.Option("--show-code", help="Print the generated filter code before running it."),
    ] = False,
    save: Annotated[
        Path | None,
        typer.Option(help="Save the generated filter as a self-describing, replayable script."),
    ] = None,
    run: Annotated[
        Path | None,
        typer.Option(
            "--run",
            help="Replay a previously saved filter through the sandbox instead of "
            "generating one (no PROMPT, no LLM call).",
        ),
    ] = None,
    confirm: Annotated[
        bool,
        typer.Option(
            "--confirm",
            "-i",
            help="Show the generated code and ask for confirmation before running it.",
        ),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output results as JSON (path plus any extra fields)."),
    ] = False,
    fields: Annotated[
        bool,
        typer.Option(
            "--fields",
            "-f",
            help="Show each result's extra per-path fields as key=value (a list-valued "
            "field renders as its count). Prints bare paths when the prompt asks for none.",
        ),
    ] = False,
    extract: Annotated[
        bool,
        typer.Option(
            "--extract",
            help="Explode each result's list-valued field into one match per line "
            "(path[:line]<TAB>payload), and steer generation to produce such a field. "
            "Selects items inside files rather than whole files. Mutually exclusive with "
            "--fields; --json stays nested.",
        ),
    ] = False,
    extract_field: Annotated[
        str | None,
        typer.Option(
            "--extract-field",
            metavar="NAME",
            help="With --extract, name the list-valued field to explode when a record "
            "has more than one. Requires --extract.",
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Approve any requested packages without prompting."),
    ] = False,
    no_deps: Annotated[
        bool,
        typer.Option("--no-deps", help="Reject any third-party packages (standard library only)."),
    ] = False,
    no_format: Annotated[
        bool,
        typer.Option(
            "--no-format",
            help="Skip the ruff cleanup (remove unused imports, sort imports, format) "
            "applied to the generated filter.",
        ),
    ] = False,
    macos_meta: Annotated[
        bool,
        typer.Option(
            "--macos-meta",
            help="macOS only: expose Finder tags and download (quarantine/where-from) "
            "metadata to the filter.",
        ),
    ] = False,
    exclude: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude",
            metavar="GLOB",
            help="Glob of names/paths to skip during enumeration (matching directories "
            "are pruned). Repeatable, e.g. --exclude '*.min.js' --exclude build.",
        ),
    ] = None,
    no_ignore: Annotated[
        bool,
        typer.Option(
            "--no-ignore",
            help="Walk the complete tree instead of skipping default ignored names "
            "(.git, node_modules, __pycache__, .venv, caches, …).",
        ),
    ] = False,
    max_depth: Annotated[
        int | None,
        typer.Option(
            "--max-depth",
            metavar="N",
            help="Descend at most N directory levels below PATH (a direct child is 1).",
        ),
    ] = None,
    print0: Annotated[
        bool,
        typer.Option(
            "--print0",
            "-0",
            help="Separate results with NUL bytes instead of newlines (for 'xargs -0'); "
            "safe for paths containing spaces or newlines.",
        ),
    ] = False,
    max_results: Annotated[
        int | None,
        typer.Option(help="Return at most N path records; complete results only."),
    ] = None,
    max_items: Annotated[
        int | None,
        typer.Option(help="With --extract, emit at most N extracted item rows."),
    ] = None,
    max_output_bytes: Annotated[
        int | None,
        typer.Option(help="Write at most N encoded stdout bytes; never partial rows or JSON."),
    ] = None,
) -> None:
    """Search PATH for files matching PROMPT and print one path per line."""
    try:
        request = plan_command(
            prompt=prompt,
            paths=paths,
            list_models=list_models,
            model=model,
            run=run,
            save=save,
            confirm=confirm,
            macos_meta=macos_meta,
            as_json=as_json,
            fields=fields,
            print0=print0,
            extract=extract,
            extract_field=extract_field,
            yes=yes,
            no_deps=no_deps,
            max_depth=max_depth,
            command_timeout=command_timeout,
            max_results=max_results,
            max_items=max_items,
            max_output_bytes=max_output_bytes,
        )
    except (TimeoutError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1 if isinstance(exc, TimeoutError) else 2) from exc

    try:
        cancel_deadline = arm_command_timeout(command_timeout)
    except (TimeoutError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1 if isinstance(exc, TimeoutError) else 2) from exc
    ctx.call_on_close(cancel_deadline)

    try:
        request, stdin_no_paths = _resolve_stdin_paths(request)
    except (TimeoutError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1 if isinstance(exc, TimeoutError) else 2) from exc
    if stdin_no_paths:
        _emit(
            [],
            as_json=as_json,
            fields=fields,
            print0=print0,
            extract=extract,
            extract_field=extract_field,
            max_results=max_results,
            max_items=max_items,
            max_output_bytes=max_output_bytes,
        )
        raise typer.Exit(0)

    if isinstance(request, ListModelsRequest):
        try:
            for model_id in backend.list_models(request.model):
                typer.echo(model_id)
        except (TimeoutError, RuntimeError, ValueError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        raise typer.Exit(0)

    try:
        sandbox_backend_value = _validate_sandbox_backend(sandbox_backend)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    _warn_if_experimental_sandbox(sandbox_backend_value)

    if isinstance(request, GeneratedSearchRequest) and macos_meta and sys.platform != "darwin":
        typer.echo("warning: --macos-meta is ignored on non-macOS hosts.", err=True)

    generate_only_mode = isinstance(request, GeneratedSearchRequest) and not request.paths
    if generate_only_mode and not (show_code or save is not None or confirm):
        typer.echo(
            "warning: no PATH given and no --save, --show-code, or --confirm — "
            "the generated filter will be discarded.",
            err=True,
        )

    def on_generated(generated: GeneratedFilter) -> None:
        if save is not None:
            plan_prompt = request.prompt if isinstance(request, GeneratedSearchRequest) else ""
            save.write_text(backend.serialize_filter(generated, plan_prompt, model))
            typer.echo(f"saved generated filter to {save}", err=True)
        if show_code or confirm:
            typer.echo(f"--- generated filter ({generated.runtime}) ---", err=True)
            typer.echo(_highlight(generated.code, generated.runtime), err=True)
            typer.echo("------------------------", err=True)
        if confirm and not typer.confirm("Run this filter?", default=False, err=True):
            typer.echo("aborted.", err=True)
            raise typer.Exit(130)

    def approve_dependencies(packages: list[str]) -> bool:
        listed = ", ".join(packages)
        if no_deps:
            typer.echo(f"refusing new packages (--no-deps): {listed}", err=True)
            return False
        typer.echo(
            f"The generated filter needs these packages installed in the sandbox: {listed}",
            err=True,
        )
        if yes:
            return True
        return typer.confirm("Install and remember them?", default=False, err=True)

    def on_retry(retry: int, error: ValueError) -> None:
        typer.echo(f"generation attempt failed, retrying (retry {retry}): {error}", err=True)

    needs_hook = show_code or save is not None or confirm or generate_only_mode
    hook = on_generated if needs_hook else None
    exclude_globs = tuple(exclude or ())
    use_default_ignores = not no_ignore

    try:
        if isinstance(request, SavedReplayRequest):
            results = backend.run_saved(
                request.filter_path,
                request.paths,
                image=image,
                sandbox_backend=sandbox_backend_value,
                timeout=timeout,
                memory=memory,
                cpus=cpus,
                pids_limit=pids_limit,
                rebuild=rebuild,
                build_timeout=build_timeout,
                approve_dependencies=approve_dependencies,
                on_generated=hook,
                exclude=exclude_globs,
                max_depth=max_depth,
                use_default_ignores=use_default_ignores,
            )
        elif generate_only_mode:
            assert isinstance(request, GeneratedSearchRequest)
            backend.generate_only(
                request.prompt,
                model=model,
                on_generated=hook,
                on_retry=on_retry,
                macos_meta=macos_meta,
                extract=extract,
                format_code=not no_format,
            )
            raise typer.Exit(0)
        else:
            assert isinstance(request, GeneratedSearchRequest)
            results = backend.search(
                request.paths,
                request.prompt,
                image=image,
                sandbox_backend=sandbox_backend_value,
                model=model,
                timeout=timeout,
                memory=memory,
                cpus=cpus,
                pids_limit=pids_limit,
                rebuild=rebuild,
                build_timeout=build_timeout,
                on_generated=hook,
                on_retry=on_retry,
                approve_dependencies=approve_dependencies,
                macos_meta=macos_meta,
                extract=extract,
                format_code=not no_format,
                exclude=exclude_globs,
                max_depth=max_depth,
                use_default_ignores=use_default_ignores,
            )
    except (typer.Exit, typer.Abort):
        # Control-flow exceptions (e.g. a declined --confirm) subclass RuntimeError;
        # let them propagate to Typer instead of reporting them as errors.
        raise
    except (DockerError, TimeoutError, RuntimeError, ValueError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc

    try:
        _emit(
            results,
            as_json=as_json,
            fields=fields,
            print0=print0,
            extract=extract,
            extract_field=extract_field,
            max_results=max_results,
            max_items=max_items,
            max_output_bytes=max_output_bytes,
        )
    except (TimeoutError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


if __name__ == "__main__":
    app()
