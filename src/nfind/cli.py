"""Command-line interface for nfind."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from . import backend
from .backend import (
    DEFAULT_BUILD_TIMEOUT,
    DEFAULT_MODEL,
    DockerError,
    GeneratedFilter,
)
from .command_plan import (
    GeneratedSearchRequest,
    ListModelsRequest,
    SavedReplayRequest,
    plan_command,
)
from .config import ConfigError, default_config_path, load_config

app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Find files by describing them in natural language.",
)


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


def _emit(records: list[dict[str, Any]], *, as_json: bool, verbose: bool, print0: bool) -> None:
    """Render result records in the requested output mode.

    Default: one path per line. ``--print0``: paths separated by NUL bytes (for
    ``xargs -0``). ``--json``: a JSON object with count and the full records (path plus
    any extra fields). ``--verbose``: each path followed by its extra fields, when the
    filter produced any.
    """
    if as_json:
        typer.echo(json.dumps({"count": len(records), "results": records}, indent=2))
        return
    if print0:
        # NUL-terminate each path (the find -print0 / xargs -0 convention) so paths
        # containing spaces or newlines survive the pipeline intact.
        sys.stdout.write("".join(f"{record['path']}\0" for record in records))
        return
    for record in records:
        path = record["path"]
        extras = {key: value for key, value in record.items() if key != "path"}
        if verbose and extras:
            detail = ", ".join(f"{key}={value}" for key, value in extras.items())
            typer.echo(f"{path}\t{detail}")
        else:
            typer.echo(path)


@app.command(no_args_is_help=True)
def main(
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
            help="One or more directories to search (default: current directory). "
            "With several, each is searched and results are merged.",
        ),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config",
            envvar="NFIND_CONFIG",
            is_eager=True,
            callback=_load_config_defaults,
            help="TOML config file supplying defaults for options (model, timeout, "
            "memory, cpus, pids-limit, build-timeout, image, json, verbose, no-format). "
            "Defaults to config.toml in nfind's per-user config directory; "
            "command-line options win.",
        ),
    ] = None,
    model: Annotated[
        str,
        typer.Option(
            help="Model used to generate the filter. Bare name uses OpenAI; use "
            "'provider/model' for others (e.g. anthropic/claude-3-5-sonnet-latest, "
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
    timeout: Annotated[
        float,
        typer.Option(help="Seconds the generated filter may run before it is killed."),
    ] = 10.0,
    memory: Annotated[
        str,
        typer.Option(help="Memory limit for the worker container (e.g. 256m)."),
    ] = "256m",
    cpus: Annotated[
        float,
        typer.Option(help="CPU limit for the worker container."),
    ] = 1.0,
    pids_limit: Annotated[
        int,
        typer.Option(help="Maximum number of processes inside the worker container."),
    ] = 64,
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
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show extra per-path fields alongside each path."),
    ] = False,
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
            help="Don't skip the default ignored directories (.git, node_modules, "
            "__pycache__, .venv, caches, …).",
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
            verbose=verbose,
            print0=print0,
            yes=yes,
            no_deps=no_deps,
            max_depth=max_depth,
        )
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    if isinstance(request, ListModelsRequest):
        try:
            for model_id in backend.list_models(request.model):
                typer.echo(model_id)
        except (RuntimeError, ValueError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        raise typer.Exit(0)

    if isinstance(request, GeneratedSearchRequest) and macos_meta and sys.platform != "darwin":
        typer.echo("warning: --macos-meta is ignored on non-macOS hosts.", err=True)

    def on_generated(generated: GeneratedFilter) -> None:
        if save is not None:
            plan_prompt = request.prompt if isinstance(request, GeneratedSearchRequest) else ""
            save.write_text(backend.serialize_filter(generated, plan_prompt, model))
            typer.echo(f"saved generated filter to {save}", err=True)
        if show_code or confirm:
            # Show the full script as --save would write it. On a --run replay the
            # code already is that full file, so show it as-is (no double-wrapping).
            preview = (
                generated.code
                if isinstance(request, SavedReplayRequest)
                else backend.serialize_filter(generated, request.prompt, model)
            )
            typer.echo(f"--- generated filter ({generated.runtime}) ---", err=True)
            typer.echo(_highlight(preview, generated.runtime), err=True)
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
        if verbose:
            typer.echo(f"generation attempt failed, retrying (retry {retry}): {error}", err=True)

    hook = on_generated if (show_code or save is not None or confirm) else None
    exclude_globs = tuple(exclude or ())
    use_default_ignores = not no_ignore

    try:
        if isinstance(request, SavedReplayRequest):
            results = backend.run_saved(
                request.filter_path,
                request.paths,
                image=image,
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
        else:
            assert isinstance(request, GeneratedSearchRequest)
            results = backend.search(
                request.paths,
                request.prompt,
                image=image,
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
                format_code=not no_format,
                exclude=exclude_globs,
                max_depth=max_depth,
                use_default_ignores=use_default_ignores,
            )
    except (typer.Exit, typer.Abort):
        # Control-flow exceptions (e.g. a declined --confirm) subclass RuntimeError;
        # let them propagate to Typer instead of reporting them as errors.
        raise
    except (DockerError, TimeoutError, RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc

    _emit(results, as_json=as_json, verbose=verbose, print0=print0)


if __name__ == "__main__":
    app()
