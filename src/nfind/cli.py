"""Command-line interface for nfind."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any, cast

import click
import tomlkit
import typer

from . import __version__, backend
from . import sandbox as sandbox_module
from .backend import (
    DEFAULT_BUILD_TIMEOUT,
    DEFAULT_MODEL,
    DEFAULT_SANDBOX_BACKEND,
    CacheEntry,
    DockerError,
    GeneratedFilter,
    QueryCache,
    SandboxBackend,
)
from .command_plan import (
    CommandRequest,
    GeneratedSearchRequest,
    ListModelsRequest,
    SavedReplayRequest,
    plan_command,
)
from .config import (
    ConfigError,
    configurable_param_names,
    default_config_path,
    known_config_keys,
    load_config,
    normalize_config_key,
    parse_config_value,
    resolved_config_path,
)
from .constants import (
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_CPUS,
    DEFAULT_MEMORY,
    DEFAULT_PIDS_LIMIT,
    DEFAULT_TIMEOUT,
)
from .deadline import arm_command_timeout
from .embedding import DEFAULT_EMBEDDING_MODEL, build_embedder
from .extract import iter_extract_rows
from .query_cache import DEFAULT_SEMANTIC_THRESHOLD, iter_entry_summaries

_DEFAULT_COMMAND = "search"


# Grouping of the search command's options into Rich help panels (by click param name).
# Kept in one place so the panels stay coherent; options not listed here (``--version``,
# ``--config``, ``--help``) fall in Typer's default "Options" panel. Applied to the built
# command in :func:`_style_search_help` because the ``Annotated[..., typer.Option(...)]``
# metadata is re-evaluated on every build (no persistent option object to set earlier).
_OPTION_PANELS: dict[str, str] = {
    "model": "Model",
    "list_models": "Model",
    "image": "Sandbox & resources",
    "sandbox_backend": "Sandbox & resources",
    "timeout": "Sandbox & resources",
    "command_timeout": "Sandbox & resources",
    "memory": "Sandbox & resources",
    "cpus": "Sandbox & resources",
    "pids_limit": "Sandbox & resources",
    "rebuild": "Sandbox & resources",
    "build_timeout": "Sandbox & resources",
    "exclude": "Search scope",
    "no_ignore": "Search scope",
    "max_depth": "Search scope",
    "macos_meta": "Search scope",
    "show_code": "Generated filter",
    "save": "Generated filter",
    "run": "Generated filter",
    "confirm": "Generated filter",
    "no_format": "Generated filter",
    "yes": "Dependencies",
    "no_deps": "Dependencies",
    "as_json": "Output",
    "fields": "Output",
    "extract": "Output",
    "extract_field": "Output",
    "print0": "Output",
    "max_results": "Output",
    "max_items": "Output",
    "max_output_bytes": "Output",
    "cache": "Query cache",
    "force": "Query cache",
}


def _style_search_help(command: Any) -> None:
    """Tag configurable options and sort the search options into Rich help panels.

    Applied to the built Click command as the group resolves it (the
    ``Annotated[..., typer.Option(...)]`` metadata is re-evaluated on every build, so there
    is no persistent option object to style earlier). The ``(config)`` marks are driven off
    the config schema (:func:`configurable_param_names`) so they can't drift. Idempotent.
    """
    configurable = configurable_param_names()
    for param in command.params:
        # Match on Click's param_type_name rather than isinstance(param, click.Option):
        # Typer 0.26 vendors click, so its options are not real click.Option instances.
        if getattr(param, "param_type_name", None) != "option":
            continue
        panel = _OPTION_PANELS.get(param.name)
        if panel is not None:
            param.rich_help_panel = panel
        help_text = getattr(param, "help", None)
        if param.name in configurable and help_text and not help_text.endswith("(config)"):
            param.help = f"{help_text}  (config)"


class DefaultCommandGroup(typer.core.TyperGroup):
    """A Typer group whose default subcommand is the plain ``search`` filter command.

    nfind is primarily a single verb -- ``nfind "prompt" PATH`` -- so the ``search``
    command runs when the first token is not a known subcommand (e.g. ``cache``). Top-level
    help (and a bare ``nfind``) shows the ``search`` command's full help, keeping the
    familiar flat interface while ``nfind cache ...`` gets its own namespace.
    """

    # ctx is typed Any so the override stays compatible across typer versions (older
    # typer passes a click.Context, newer a vendored typer._click Context); it is only
    # forwarded to super().parse_args.
    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        if not args:
            # Bare `nfind`: defer to the search command's own no-args help.
            args = [_DEFAULT_COMMAND]
        elif args in (["-h"], ["--help"]):
            # Top-level help == the primary (search) command's full help.
            args = [_DEFAULT_COMMAND, *args]
        elif args[0] not in self.commands:
            args = [_DEFAULT_COMMAND, *args]
        return super().parse_args(ctx, args)

    def get_command(self, ctx: Any, cmd_name: str) -> Any:
        # Tag the search command's configurable options as help is resolved. Both the real
        # CLI and CliRunner reach the search command through here, so the marks show up
        # consistently without hand-editing each option's help string.
        command = super().get_command(ctx, cmd_name)
        if cmd_name == _DEFAULT_COMMAND and command is not None:
            _style_search_help(command)
        return command


class SearchCommand(typer.core.TyperCommand):
    """The default ``search`` command, whose help also advertises nfind's subcommands.

    Because ``nfind --help`` renders this single command's help (not the group's), Typer
    draws no "Commands" panel. This appends one listing the sibling subcommands
    (``cache``, ``config``) so they are as visible as they would be on a group's help.
    """

    def format_help(self, ctx: Any, formatter: Any) -> None:
        super().format_help(ctx, formatter)
        siblings = self._sibling_subcommands(ctx)
        if not siblings:
            return
        try:
            if self.rich_markup_mode is None:
                raise ImportError  # Rich disabled -> use the plain formatter path below.
            from typer import rich_utils

            print_panel = rich_utils._print_commands_panel
            get_console = rich_utils._get_rich_console
            print_panel(
                name="Subcommands",
                commands=siblings,
                markup_mode=self.rich_markup_mode,
                console=get_console(),
                cmd_len=max(len(c.name or "") for c in siblings),
            )
        except (ImportError, AttributeError):
            # No Rich (or its private helper moved): fall back to a plain section.
            with formatter.section("Subcommands"):
                formatter.write_dl([(c.name or "", c.get_short_help_str()) for c in siblings])

    def _sibling_subcommands(self, ctx: Any) -> list[Any]:
        """The other subcommands registered on the parent group (cache, config)."""
        group = getattr(getattr(ctx, "parent", None), "command", None)
        commands = getattr(group, "commands", {})
        return [
            command
            for name, command in commands.items()
            if name != self.name and not getattr(command, "hidden", False)
        ]


app = typer.Typer(
    cls=DefaultCommandGroup,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Find files by describing them in natural language.",
)

cache_app = typer.Typer(
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Manage the cache of past prompts and their generated filters.",
)
app.add_typer(cache_app, name="cache")

config_app = typer.Typer(
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Locate, inspect, and edit nfind's configuration file.",
)
app.add_typer(config_app, name="config")

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
    "Docker, including full network isolation via --network none (unlike Apple Containers "
    "on macOS 15), and on rootless Podman remaps the read-only mount to the worker user so "
    "it stays readable. It has been validated only on limited hosts, and rootless isolation "
    "differs from a rootful Docker daemon. Use Docker for the most thoroughly tested sandbox."
)

_NERDCTL_SANDBOX_WARNING = (
    "warning: nerdctl sandbox is experimental; nfind applies the same hardening flags as "
    "Docker, including full network isolation via --network none. It is validated end-to-end "
    "on Linux CI against rootful containerd, but on rootless nerdctl the read-only mount may "
    "be unreadable by the non-root worker (unlike rootless Podman, nerdctl has no keep-id "
    "remap), so prefer rootful containerd. Use Docker for the most thoroughly tested sandbox."
)


def _print_version(value: bool) -> None:
    """Print the nfind version and exit (eager --version callback)."""
    if value:
        typer.echo(f"nfind {__version__}")
        raise typer.Exit()


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
    elif sandbox_backend == "nerdctl":
        typer.echo(_NERDCTL_SANDBOX_WARNING, err=True)


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


def _build_query_cache(
    *,
    enabled: bool,
    semantic: bool,
    embedding_model: str | None,
    threshold: float | None,
) -> QueryCache | None:
    """Construct the query cache for a search, or ``None`` when caching is disabled.

    Semantic matching is engaged only when requested *and* the optional ``sqlite-vec``
    dependency is importable; otherwise a warning is printed and the cache falls back to
    normalized-exact matching, which needs nothing beyond the standard library.
    """
    if not enabled:
        return None
    embedder = None
    canonical_model = None
    if semantic:
        if importlib.util.find_spec("sqlite_vec") is None:
            typer.echo(
                "warning: semantic cache matching (config 'cache-semantic') needs the "
                "optional 'semantic' extra (pip install 'nfind[semantic]'); "
                "using exact matching instead.",
                err=True,
            )
        else:
            try:
                canonical_model, embed = build_embedder(embedding_model or DEFAULT_EMBEDDING_MODEL)
            except (RuntimeError, ValueError) as exc:
                typer.echo(f"warning: semantic cache disabled: {exc}", err=True)
            else:
                embedder = cast(Any, embed)
    return QueryCache(
        embedder=embedder,
        embedding_model=canonical_model,
        threshold=threshold if threshold is not None else DEFAULT_SEMANTIC_THRESHOLD,
    )


@cache_app.command("list")
def cache_list() -> None:
    """List stored prompts and the filters generated for them, newest first."""
    with QueryCache() as cache:
        entries = cache.all()
    if not entries:
        typer.echo("cache is empty.", err=True)
        raise typer.Exit(0)
    for line in iter_entry_summaries(entries):
        typer.echo(line)


@cache_app.command("show")
def cache_show(
    entry_id: Annotated[
        int, typer.Argument(metavar="ID", help="Cache entry id (see 'cache list').")
    ],
) -> None:
    """Show one cached entry's prompt, provenance, and generated filter code."""
    with QueryCache() as cache:
        entry = cache.get(entry_id)
    if entry is None:
        typer.echo(f"error: no cache entry with id {entry_id}", err=True)
        raise typer.Exit(1)
    mode = []
    if entry.macos_meta:
        mode.append("macos-meta")
    if entry.extract:
        mode.append("extract")
    typer.echo(f"id:      {entry.id}", err=True)
    typer.echo(f"prompt:  {entry.prompt}", err=True)
    typer.echo(f"model:   {entry.model}", err=True)
    typer.echo(f"runtime: {entry.runtime}", err=True)
    if entry.dependencies:
        typer.echo(f"deps:    {', '.join(entry.dependencies)}", err=True)
    if mode:
        typer.echo(f"mode:    {', '.join(mode)}", err=True)
    typer.echo(f"created: {entry.created_at}", err=True)
    used = f"{entry.used_count}x" + (f", last {entry.last_used_at}" if entry.last_used_at else "")
    typer.echo(f"used:    {used}", err=True)
    typer.echo("--- filter code ---", err=True)
    typer.echo(_highlight(entry.code, entry.runtime))


@cache_app.command("delete")
def cache_delete(
    entry_ids: Annotated[
        list[int],
        typer.Argument(metavar="ID...", help="Cache entry id(s) to delete (see 'cache list')."),
    ],
) -> None:
    """Delete one or more cache entries by id. Remaining entries keep their ids."""
    with QueryCache() as cache:
        present = {entry.id for entry in cache.all()}
        missing = [i for i in dict.fromkeys(entry_ids) if i not in present]
        removed = cache.delete(entry_ids)
    if missing:
        ids = ", ".join(str(i) for i in missing)
        typer.echo(f"warning: no cache entry with id {ids}", err=True)
    noun = "entry" if removed == 1 else "entries"
    typer.echo(f"deleted {removed} cache {noun}.", err=True)
    if removed == 0:
        raise typer.Exit(1)


@cache_app.command("clear")
def cache_clear(
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Clear without confirmation."),
    ] = False,
) -> None:
    """Delete every stored prompt/filter pair."""
    with QueryCache() as cache:
        entries = cache.all()
        if not entries:
            typer.echo("cache is already empty.", err=True)
            raise typer.Exit(0)
        if not yes and not typer.confirm(
            f"Delete all {len(entries)} cached entries?", default=False, err=True
        ):
            typer.echo("aborted.", err=True)
            raise typer.Exit(130)
        removed = cache.clear()
    typer.echo(f"cleared {removed} cached entries.", err=True)


def _format_toml_value(value: Any) -> str:
    """Render a parsed TOML value for `config get`: TOML booleans, one list item per line."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return str(value)


def _config_template() -> str:
    """A commented config file listing every key with its built-in default (all disabled).

    Values are interpolated from the same constants the runtime uses, so the template can't
    drift from the real defaults. Every line is commented out, so writing it changes no
    behaviour until the user uncomments a key.
    """
    return f"""\
# nfind configuration file
#
# Every setting below is OPTIONAL: nfind runs with no config file, using the built-in
# defaults shown here. Uncomment a line to override that default; command-line options
# always win over this file. Keys mirror the CLI option names (the underscore spelling,
# e.g. pids_limit, also works). See https://deeplook.github.io/nfind/configuration/

# -- Model & provider --------------------------------------------------------
# model = "{DEFAULT_MODEL}"           # bare name = OpenAI; otherwise provider/model

# -- Sandbox & resources -----------------------------------------------------
# sandbox = "{DEFAULT_SANDBOX_BACKEND}"                 # docker | apple | podman | nerdctl
# image = "registry/name:tag"      # override the runtime's base image (default: per-runtime)
# timeout = {DEFAULT_TIMEOUT:g}                  # seconds the generated filter may run
# command-timeout = 300            # whole-command deadline, seconds (default: unlimited)
# memory = "{DEFAULT_MEMORY}"                # worker memory limit
# cpus = {DEFAULT_CPUS:g}                       # worker CPU limit
# pids-limit = {DEFAULT_PIDS_LIMIT}                # max processes inside the worker
# build-timeout = {DEFAULT_BUILD_TIMEOUT:g}            # seconds allowed to build the worker image

# -- Enumeration -------------------------------------------------------------
# exclude = ["vendor", "*.min.js"] # globs to prune before filtering (default: none)
# no-ignore = false                # also walk .git, node_modules, caches, …
# max-depth = 6                    # limit traversal depth (default: unlimited)

# -- Output ------------------------------------------------------------------
# json = false
# fields = false
# print0 = false
# show-code = false                # print the generated filter before running it
# no-format = false                # skip the ruff cleanup of the generated filter
# max-results = 100                # cap path records (default: unlimited)
# max-items = 100                  # cap --extract rows (default: unlimited)
# max-output-bytes = 1048576       # cap stdout bytes (default: unlimited)

# -- Query cache -------------------------------------------------------------
# cache = true                     # reuse and store generated filters
# cache-semantic = false           # also reuse similar prompts (needs the 'semantic' extra)
# cache-embedding-model = "{DEFAULT_EMBEDDING_MODEL}"
# cache-threshold = {DEFAULT_SEMANTIC_THRESHOLD}              # max cosine distance for a reuse
"""


@config_app.command("init")
def config_init(
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Overwrite an existing config file."),
    ] = False,
) -> None:
    """Write a commented config-file template (every key with its default, all disabled)."""
    path = resolved_config_path()
    if path.is_file() and not force:
        typer.echo(
            f"error: config file already exists at {path} (use --force to overwrite).",
            err=True,
        )
        raise typer.Exit(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_config_template(), encoding="utf-8")
    typer.echo(f"wrote config template to {path}", err=True)


@config_app.command("path")
def config_path() -> None:
    """Print the path of the config file nfind reads (whether or not it exists)."""
    typer.echo(str(resolved_config_path()))


@config_app.command("show")
def config_show() -> None:
    """Print the current config file, or note that none exists."""
    path = resolved_config_path()
    if not path.is_file():
        typer.echo(f"no config file at {path} (using built-in defaults).", err=True)
        raise typer.Exit(0)
    typer.echo(path.read_text(encoding="utf-8").rstrip("\n"))


@config_app.command("get")
def config_get(
    key: Annotated[
        str,
        typer.Argument(metavar="KEY", help="Config key, e.g. model or cache-threshold."),
    ],
) -> None:
    """Print the value set for KEY in the config file (built-in default applies if unset)."""
    normalized = key.replace("_", "-")
    if normalized not in known_config_keys():
        valid = ", ".join(known_config_keys())
        typer.echo(f"error: unknown config key {key!r}. Valid keys: {valid}", err=True)
        raise typer.Exit(2)
    path = resolved_config_path()
    if not path.is_file():
        typer.echo(f"{normalized} is unset (no config file); using the built-in default.", err=True)
        raise typer.Exit(0)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    # A key may be written with either the hyphen or underscore spelling.
    for candidate in (normalized, normalized.replace("-", "_")):
        if candidate in data:
            typer.echo(_format_toml_value(data[candidate]))
            return
    typer.echo(f"{normalized} is unset; using the built-in default.", err=True)


@config_app.command("edit")
def config_edit() -> None:
    """Open the config file in your $EDITOR (creating its directory if needed)."""
    path = resolved_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    click.edit(filename=str(path))


def _load_toml_document(path: Path) -> tomlkit.TOMLDocument:
    """Parse the config file into a comment-preserving tomlkit document (empty if absent)."""
    if not path.is_file():
        return tomlkit.document()
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except tomlkit.exceptions.TOMLKitError as exc:
        typer.echo(f"error: could not parse config file {path}: {exc}", err=True)
        raise typer.Exit(1) from exc


def _existing_key(doc: tomlkit.TOMLDocument, normalized: str) -> str | None:
    """The spelling (hyphen or underscore) under which ``normalized`` is present, if any."""
    for candidate in (normalized, normalized.replace("-", "_")):
        if candidate in doc:
            return candidate
    return None


@config_app.command("set")
def config_set(
    key: Annotated[
        str,
        typer.Argument(metavar="KEY", help="Config key, e.g. model or cache-threshold."),
    ],
    values: Annotated[
        list[str],
        typer.Argument(
            metavar="VALUE...",
            help="Value to set. Repeat for list-valued keys, e.g. 'set exclude vendor dist'.",
        ),
    ],
) -> None:
    """Set KEY in the config file, preserving existing comments and formatting."""
    try:
        normalized = normalize_config_key(key)
        value = parse_config_value(key, list(values))
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    path = resolved_config_path()
    doc = _load_toml_document(path)
    doc[_existing_key(doc, normalized) or normalized] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    rendered = tomlkit.item(value).as_string()
    typer.echo(f"set {normalized} = {rendered} in {path}", err=True)


@config_app.command("unset")
def config_unset(
    key: Annotated[
        str,
        typer.Argument(metavar="KEY", help="Config key to remove from the file."),
    ],
) -> None:
    """Remove KEY from the config file (reverting it to nfind's built-in default)."""
    try:
        normalized = normalize_config_key(key)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    path = resolved_config_path()
    if not path.is_file():
        typer.echo(f"{normalized} is not set (no config file at {path}).", err=True)
        raise typer.Exit(0)
    doc = _load_toml_document(path)
    target = _existing_key(doc, normalized)
    if target is None:
        typer.echo(f"{normalized} is not set; nothing to do.", err=True)
        raise typer.Exit(0)
    del doc[target]
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    typer.echo(f"unset {normalized} in {path}", err=True)


@app.command(
    _DEFAULT_COMMAND,
    cls=SearchCommand,
    no_args_is_help=True,
)
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
            '"..." -\'). Defaults to the current directory when omitted; with '
            "--save/--show-code/--confirm and no PATH, the filter is generated but not run.",
        ),
    ] = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            is_eager=True,
            callback=_print_version,
            help="Show the nfind version and exit.",
        ),
    ] = False,
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
            "podman (experimental), or nerdctl (containerd, experimental).",
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
    cache: Annotated[
        bool,
        typer.Option(
            "--cache/--no-cache",
            help="Reuse and store generated filters in the on-disk query cache "
            "(prompts you have run before skip the LLM). On by default; semantic matching "
            "and its embedding model/threshold are configured in the config file.",
        ),
    ] = True,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Ignore any cached match and regenerate the filter with the LLM "
            "(the fresh result is still stored).",
        ),
    ] = False,
) -> None:
    """Search PATH for files matching PROMPT and print one path per line.

    Options tagged (config) can be set as defaults in the config file; see 'nfind config'.
    """
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

    # A prompt with no PATH defaults to the current directory, like `find`. The
    # exception is generate-only mode (--save/--show-code/--confirm without a PATH),
    # where the user wants to inspect or keep the filter without running it.
    if (
        isinstance(request, GeneratedSearchRequest)
        and not request.paths
        and not (show_code or save is not None or confirm)
    ):
        request = replace(request, paths=["."])

    generate_only_mode = isinstance(request, GeneratedSearchRequest) and not request.paths

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

    def on_cache_hit(entry: CacheEntry) -> None:
        detail = (
            f" (semantic match, distance {entry.distance:.3f})"
            if entry.distance is not None
            else ""
        )
        typer.echo(
            f"reusing cached filter #{entry.id} from {entry.created_at[:19]}{detail}; "
            "pass --force to regenerate.",
            err=True,
        )

    needs_hook = show_code or save is not None or confirm or generate_only_mode
    hook = on_generated if needs_hook else None
    exclude_globs = tuple(exclude or ())
    use_default_ignores = not no_ignore

    # The cache is only consulted on the generation paths, never for --run replay.
    query_cache: QueryCache | None = None
    if isinstance(request, GeneratedSearchRequest):
        # Semantic matching and its embedding model/threshold are set-once preferences,
        # so they come from the resolved config file rather than per-run CLI flags.
        cache_config = ctx.default_map or {}
        query_cache = _build_query_cache(
            enabled=cache,
            semantic=bool(cache_config.get("semantic", False)),
            embedding_model=cache_config.get("cache_embedding_model"),
            threshold=cache_config.get("cache_threshold"),
        )
        if query_cache is not None:
            ctx.call_on_close(query_cache.close)

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
                cache=query_cache,
                force=force,
                on_cache_hit=on_cache_hit,
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
                cache=query_cache,
                force=force,
                on_cache_hit=on_cache_hit,
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
