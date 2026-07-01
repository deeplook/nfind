# --- standalone runner (embedded verbatim into saved filters by nfind --save) --------
# Reproduces nfind's DEFAULT output when a saved filter is run via `uv run FILE [PATH]...`
# without nfind installed: each root is walked (a directory has the common ignored dirs
# pruned; a file contributes just itself), then matches are printed. --json, --fields, and
# --print0 work as in nfind. It does NOT reproduce --exclude/--max-depth/--no-ignore (those
# are not saved). nfind itself never imports this module; it only reads the source. _IGNORE is kept
# in sync with nfind.constants.DEFAULT_IGNORES by a test.

import json
import os
import sys
from collections.abc import Callable
from typing import Any

_IGNORE = {
    ".DS_Store",
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}


def _main(filter_paths: Callable[[list[str]], list[Any]]) -> None:
    args = sys.argv[1:]
    as_json = "--json" in args
    print0 = "--print0" in args or "-0" in args
    fields = not as_json and ("--fields" in args or "-f" in args)
    roots = [arg for arg in args if not arg.startswith("-")] or ["."]

    paths: list[str] = []
    for root in roots:
        if os.path.isfile(root):
            paths.append(os.path.abspath(root))
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _IGNORE]
            for name in (*dirnames, *filenames):
                paths.append(os.path.abspath(os.path.join(dirpath, name)))

    records: list[dict[str, Any]] = [
        record if isinstance(record, dict) else {"path": record} for record in filter_paths(paths)
    ]
    if as_json:
        print(json.dumps({"count": len(records), "results": records}, indent=2))
        return
    if print0:
        # NUL-terminate each path (the find -print0 / xargs -0 convention).
        sys.stdout.write("".join(f"{record['path']}\0" for record in records))
        return
    for record in records:
        extras = {key: value for key, value in record.items() if key != "path"}
        if fields and extras:
            detail = ", ".join(
                f"{key}={len(value)}" if isinstance(value, list) else f"{key}={value}"
                for key, value in extras.items()
            )
            print(f"{record['path']}\t{detail}")
        else:
            print(record["path"])
