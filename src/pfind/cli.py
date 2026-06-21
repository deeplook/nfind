"""Command-line interface for pfind."""

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


def _emit(records: list[dict[str, Any]], *, as_json: bool, verbose: bool) -> None:
    """Render result records in the requested output mode.

    Default: one path per line. ``--json``: a JSON object with count and the full
    records (path plus any extra fields). ``--verbose``: each path followed by its
    extra fields, when the filter produced any.
    """
    if as_json:
        typer.echo(json.dumps({"count": len(records), "results": records}, indent=2))
        return
    for record in records:
        path = record["path"]
        extras = {key: value for key, value in record.items() if key != "path"}
        if verbose and extras:
            detail = ", ".join(f"{key}={value}" for key, value in extras.items())
            typer.echo(f"{path}\t{detail}")
        else:
            typer.echo(path)


@app.command()
def main(
    prompt: Annotated[
        str | None,
        typer.Argument(
            help="Natural-language description of the paths to find. "
            "Omit when replaying a saved filter with --run.",
        ),
    ] = None,
    path: Annotated[
        str,
        typer.Argument(help="Directory to search."),
    ] = ".",
    model: Annotated[
        str,
        typer.Option(
            help="Model used to generate the filter. Bare name uses OpenAI; use "
            "'provider/model' for others (e.g. anthropic/claude-3-5-sonnet-latest, "
            "ollama/llama3.1, openrouter/<vendor>/<model>).",
        ),
    ] = DEFAULT_MODEL,
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
    macos_meta: Annotated[
        bool,
        typer.Option(
            "--macos-meta",
            help="macOS only: expose Finder tags and download (quarantine/where-from) "
            "metadata to the filter.",
        ),
    ] = False,
) -> None:
    """Search PATH for files matching PROMPT and print one path per line."""
    if as_json and verbose:
        typer.echo("error: --json and --verbose are mutually exclusive.", err=True)
        raise typer.Exit(2)
    if yes and no_deps:
        typer.echo("error: --yes and --no-deps are mutually exclusive.", err=True)
        raise typer.Exit(2)
    if macos_meta and sys.platform != "darwin":
        typer.echo("warning: --macos-meta is ignored on non-macOS hosts.", err=True)
    if run is not None:
        # With --run there is no PROMPT, so a single positional is the search PATH.
        # Typer binds the first positional to `prompt`; shift it over to `path`.
        if prompt is not None and path == ".":
            path, prompt = prompt, None
        if prompt is not None:
            typer.echo(
                "error: with --run, pass only the search PATH (the filter is replayed, "
                "there is no PROMPT).",
                err=True,
            )
            raise typer.Exit(2)
        for flag, used in (
            ("--save", save is not None),
            ("--confirm", confirm),
            ("--macos-meta", macos_meta),
        ):
            if used:
                typer.echo(f"error: {flag} cannot be combined with --run.", err=True)
                raise typer.Exit(2)
    elif prompt is None:
        typer.echo("error: PROMPT is required (or use --run to replay a saved filter).", err=True)
        raise typer.Exit(2)

    def on_generated(generated: GeneratedFilter) -> None:
        if save is not None:
            save.write_text(backend.render_saved_filter(generated, prompt or "", model))
            typer.echo(f"saved generated filter to {save}", err=True)
        if show_code or confirm:
            # Show the full script as --save would write it. On a --run replay the
            # code already is that full file, so show it as-is (no double-wrapping).
            preview = (
                generated.code
                if run is not None
                else backend.render_saved_filter(generated, prompt or "", model)
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

    try:
        if run is not None:
            results = backend.run_saved(
                run,
                path,
                image=image,
                timeout=timeout,
                memory=memory,
                cpus=cpus,
                pids_limit=pids_limit,
                rebuild=rebuild,
                build_timeout=build_timeout,
                approve_dependencies=approve_dependencies,
                on_generated=hook,
            )
        else:
            assert prompt is not None  # guaranteed by the validation above
            results = backend.search(
                path,
                prompt,
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
            )
    except (typer.Exit, typer.Abort):
        # Control-flow exceptions (e.g. a declined --confirm) subclass RuntimeError;
        # let them propagate to Typer instead of reporting them as errors.
        raise
    except (DockerError, TimeoutError, RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc

    _emit(results, as_json=as_json, verbose=verbose)


if __name__ == "__main__":
    app()
