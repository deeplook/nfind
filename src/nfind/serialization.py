"""Serialization of generated filters to and from self-describing replay scripts.

``serialize_filter`` writes a filter as a PEP 723 script (python) or a commented
source file (node); ``deserialize_filter`` reconstructs a :class:`GeneratedFilter`
from such a file. Replaying a saved filter through the sandbox lives in
``backend.run_saved``.
"""

from __future__ import annotations

import json
import re
import textwrap
from datetime import date
from pathlib import Path

from .constants import FILTER_LINE_LENGTH
from .runtimes import RUNTIMES, GeneratedFilter


def _python_harness() -> str:
    """The standalone ``__main__`` runner appended to saved python filters.

    The body is :mod:`nfind.filter_harness` (a real, linted, tested module) read
    verbatim, followed by a call that hands the saved ``filter_paths`` to its ``_main``.
    Keeping it a module rather than an inline string keeps it valid Python that ``ruff``
    and ``mypy`` check and tests exercise directly.
    """
    body = Path(__file__).with_name("filter_harness.py").read_text().rstrip()
    return f'{body}\n\n\nif __name__ == "__main__":\n    _main(filter_paths)\n'


_PYTHON_HARNESS = _python_harness()

# PEP 723 inline script-metadata block: `uv run` reads dependencies from here.
_SCRIPT_METADATA_RE = re.compile(
    r"^# /// script\s*$\n(?P<body>(?:^#(?: .*)?$\n)*)^# ///\s*$",
    re.MULTILINE,
)
_SCRIPT_DEP_RE = re.compile(r'"(?P<name>[^"]+)"')
_NODE_METADATA_RE = re.compile(r"^// nfind-metadata: (?P<payload>\{.*\})$", re.MULTILINE)


def _header(prompt: str, model: str, runtime: str, comment: str) -> list[str]:
    """Provenance + safety lines for a saved filter, each prefixed with ``comment``."""
    warning = (
        "WARNING: running this file directly (e.g. `uv run`) executes OUTSIDE the "
        "nfind Docker sandbox -- no read-only mount, no network block, full user "
        "privileges. Only run filters you have reviewed and trust. To replay it "
        "sandboxed instead, use `nfind --run`."
    )
    # ruff formats code but not prose, so wrap the warning paragraph ourselves to the
    # same width (accounting for the comment prefix). The aligned key/value lines are
    # left verbatim so their two-space column alignment is preserved.
    prefix_len = len(comment) + 1 if comment else 0
    warning_lines = textwrap.wrap(warning, width=FILTER_LINE_LENGTH - prefix_len)
    lines = [
        "nfind filter",
        "",
        f"Prompt:  {prompt}",
        f"Model:   {model}",
        f"Runtime: {runtime}",
        f"Saved:   {date.today().isoformat()}",
        "",
        *warning_lines,
    ]
    return [f"{comment} {line}".rstrip() for line in lines]


def serialize_filter(generated: GeneratedFilter, prompt: str, model: str) -> str:
    """Render a generated filter as a self-describing, replayable script.

    For the python runtime the result is a PEP 723 script: a ``# /// script`` block
    declaring the filter's dependencies, a module docstring carrying the prompt,
    provenance and a safety warning, the ``filter_paths`` source, and a ``__main__``
    harness so it runs via ``uv run FILE [PATH]`` outside the sandbox.

    For the node runtime (which has no uv/PEP 723 equivalent) the result is the raw
    ``filterPaths`` source preceded by a ``//`` provenance/safety comment block and a
    machine-readable metadata line carrying its npm dependencies. Either form can be
    replayed through the sandbox with :func:`backend.run_saved`.
    """
    if generated.runtime == "node":
        header = "\n".join(_header(prompt, model, generated.runtime, "//"))
        metadata = json.dumps(
            {
                "runtime": generated.runtime,
                "dependencies": generated.dependencies,
            },
            separators=(",", ":"),
        )
        note = (
            "// Note: standalone `uv run` is python-only; replay this file sandboxed "
            "with `nfind --run`."
        )
        return f"{header}\n// nfind-metadata: {metadata}\n{note}\n\n{generated.code.rstrip()}\n"

    lines = ["# /// script", '# requires-python = ">=3.11"']
    if generated.dependencies:
        deps = ", ".join(f'"{pkg}"' for pkg in generated.dependencies)
        lines.append(f"# dependencies = [{deps}]")
    else:
        lines.append("# dependencies = []")
    lines.append("# ///")
    pep723 = "\n".join(lines)

    # Docstring carries the same provenance/warning, without the comment prefix.
    doc_lines = [line.lstrip() for line in _header(prompt, model, generated.runtime, "")]
    body = "\n".join(doc_lines).replace('"""', '\\"\\"\\"')
    docstring = f'"""\n{body}\n"""'

    return f"{pep723}\n{docstring}\n\n\n{generated.code.rstrip()}\n\n\n{_PYTHON_HARNESS}"


def deserialize_filter(source: str, *, filename: str = "") -> GeneratedFilter:
    """Reconstruct a :class:`GeneratedFilter` from a saved filter file.

    The runtime is node when the file is a ``.cjs``/``.js`` or has no PEP 723 block but
    defines ``filterPaths``; otherwise python. Dependencies are read from the PEP 723
    ``dependencies`` list (python) or the ``// nfind-metadata: ...`` line (node). Older
    node saves without metadata remain dependency-free. The full source is used as the
    filter code -- the sandbox worker extracts ``filter_paths``/``filterPaths`` and never
    runs the standalone ``__main__`` harness.
    """
    is_node = filename.endswith((".cjs", ".js")) or (
        not _SCRIPT_METADATA_RE.search(source) and "filterPaths" in source
    )
    if is_node:
        node_dependencies: list[str] = []
        if match := _NODE_METADATA_RE.search(source):
            try:
                payload = json.loads(match.group("payload"))
            except json.JSONDecodeError as exc:
                raise ValueError("invalid nfind node metadata") from exc
            if not isinstance(payload, dict):
                raise ValueError("nfind node metadata must be a JSON object")
            if payload.get("runtime", "node") != "node":
                raise ValueError("nfind node metadata has the wrong runtime")
            node_dependencies = RUNTIMES["node"].validate_packages(payload.get("dependencies", []))
        return GeneratedFilter(code=source, dependencies=node_dependencies, runtime="node")

    dependencies: list[str] = []
    match = _SCRIPT_METADATA_RE.search(source)
    if match:
        for line in match.group("body").splitlines():
            stripped = line.lstrip("#").strip()
            if stripped.startswith("dependencies"):
                _, _, rest = stripped.partition("=")
                dependencies = _SCRIPT_DEP_RE.findall(rest)
                break
    # Validate/canonicalize exactly as the node branch (and generation) do, so a crafted
    # saved file cannot smuggle pip arguments (e.g. "pkg --extra-index-url ...") through
    # the replay path into the image-build `pip install` line.
    dependencies = RUNTIMES["python"].validate_packages(dependencies)
    return GeneratedFilter(code=source, dependencies=dependencies, runtime="python")
