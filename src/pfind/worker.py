#!/usr/bin/env python3
"""In-container worker: execute an LLM-generated filter and return matching paths.

This module is intentionally self-contained -- it imports only the standard library
and is *not* part of the ``pfind`` package's import graph -- because the Docker image
copies this single file in and runs it standalone: ``python worker.py --worker`` reads
a JSON request on stdin and writes a JSON response on stdout, re-invoking itself with
``--execute-worker`` so generated-code output never pollutes the protocol stream.

The host imports the pure helpers (:func:`_normalize_results`, :data:`MAX_RESULT_BYTES`)
from here so the size limit and result-validation rules have a single source of truth.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# Maximum size, in bytes, of a worker response. The worker protocol limit lives here
# (the worker is the only standalone-shipped file); the host imports it for run_filter.
MAX_RESULT_BYTES = 1_000_000


def _normalize_results(results: Any, allowed: set[str]) -> list[dict[str, Any]]:
    """Coerce filter output into path records and verify each path was supplied.

    Accepts either a list of path strings or a list of dicts carrying a "path"
    key plus extra fields. Returns a list of dicts that always contain "path".
    """
    if not isinstance(results, list):
        raise ValueError("filter_paths must return a list.")
    records: list[dict[str, Any]] = []
    for item in results:
        if isinstance(item, str):
            record: dict[str, Any] = {"path": item}
        elif isinstance(item, dict):
            path = item.get("path")
            if not isinstance(path, str):
                raise ValueError("each result dict must have a string 'path'.")
            record = dict(item)
            record["path"] = path
        else:
            raise ValueError("filter_paths results must be strings or dicts with a 'path'.")
        if record["path"] not in allowed:
            raise ValueError("filter_paths returned a path outside its input set.")
        records.append(record)
    if len(records) > len(allowed):
        raise ValueError("filter_paths returned more results than input paths.")
    return records


def _worker_response(payload: dict[str, Any]) -> dict[str, Any]:
    code = payload.get("code")
    paths = payload.get("paths")
    if (
        not isinstance(code, str)
        or not isinstance(paths, list)
        or not all(isinstance(path, str) for path in paths)
    ):
        raise ValueError("Worker request must contain code and a list of path strings.")
    meta = payload.get("meta") or {}
    if not isinstance(meta, dict):
        raise ValueError("Worker request 'meta' must be an object.")

    # META is host-collected macOS metadata (empty unless --macos-meta). The generated
    # filter may read it via META.get(path, {}); see backend._MACOS_META_SYSTEM.
    namespace: dict[str, Any] = {"__name__": "generated_filter", "META": meta}
    # Suppress ordinary generated-code output so stdout remains a JSON protocol.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        exec(compile(code, "<generated-filter>", "exec"), namespace)  # noqa: S102
        function = namespace.get("filter_paths")
        if not callable(function):
            raise ValueError("Generated code did not define filter_paths.")
        results = function(paths)

    return {"ok": True, "results": _normalize_results(results, set(paths))}


def worker_main() -> int:
    """Container supervisor: keep generated-code output off the host protocol."""
    request = sys.stdin.buffer.read()
    response_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="response-", dir="/tmp", delete=False) as file:
            response_path = file.name
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "--execute-worker",
                response_path,
            ],
            input=request,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"filter process exited with status {completed.returncode}")
        with open(response_path, "rb") as file:
            encoded = file.read(MAX_RESULT_BYTES + 1)
        if len(encoded) > MAX_RESULT_BYTES:
            raise RuntimeError("filter response exceeded the allowed size")
        response = json.loads(encoded)
    except BaseException as exc:
        response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if response_path is not None:
            Path(response_path).unlink(missing_ok=True)

    json.dump(response, sys.stdout, separators=(",", ":"))
    return 0


def execute_worker_main(response_path: str) -> int:
    """Child entry point that executes generated code and writes a response file."""
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("Worker request must be a JSON object.")
        response = _worker_response(payload)
    except BaseException as exc:
        response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    encoded = json.dumps(response, separators=(",", ":")).encode()
    if len(encoded) > MAX_RESULT_BYTES:
        encoded = b'{"ok":false,"error":"filter response exceeded the allowed size"}'
    Path(response_path).write_bytes(encoded)
    return 0


def _module_main() -> int:
    """In-container entry point: handle only the worker dispatch modes.

    The host-facing command line lives in ``pfind.cli``. Inside the Docker image
    this file is invoked as ``python worker.py --worker`` (which in turn re-invokes
    itself with ``--execute-worker``).
    """
    if sys.argv[1:] == ["--worker"]:
        return worker_main()
    if len(sys.argv) == 3 and sys.argv[1] == "--execute-worker":
        return execute_worker_main(sys.argv[2])
    print(
        "worker.py is the in-container worker; use the 'pfind' command on the host.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(_module_main())
