"""Tests for the Node.js worker, ``src/pfind/worker_node.cjs``.

These run the worker through a real ``node`` process -- the JavaScript counterpart to
the in-process Python-worker tests in ``test_backend.py`` -- and assert its stdin/stdout
JSON protocol and error handling. They skip when node is not installed.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import pfind.backend as backend

WORKER = Path(backend.__file__).parent / "worker_node.cjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _run(request: object) -> dict:
    """Send a request to the node worker and return its parsed JSON response.

    A ``str`` request is sent verbatim (to exercise malformed input); anything else is
    JSON-encoded first.
    """
    payload = request if isinstance(request, str) else json.dumps(request)
    proc = subprocess.run(
        ["node", str(WORKER)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_returns_only_matching_input_paths():
    code = "function filterPaths(paths){ return paths.filter(p => p.endsWith('.epub')); }"
    resp = _run({"code": code, "paths": ["/data/a.epub", "/data/b.txt"]})
    assert resp == {"ok": True, "results": ["/data/a.epub"]}


def test_passes_through_object_results_with_extra_fields():
    code = "function filterPaths(paths){ return paths.map(p => ({path: p, len: p.length})); }"
    resp = _run({"code": code, "paths": ["/data/a"]})
    assert resp == {"ok": True, "results": [{"path": "/data/a", "len": 7}]}


def test_generated_stdout_does_not_corrupt_protocol():
    # console.log / process.stdout.write inside the filter must not break the JSON stream.
    code = (
        "function filterPaths(paths){ console.log('noise'); "
        "process.stdout.write('more'); return paths; }"
    )
    resp = _run({"code": code, "paths": ["/data/a"]})
    assert resp == {"ok": True, "results": ["/data/a"]}


def test_non_array_result_is_error():
    resp = _run({"code": "function filterPaths(paths){ return 'nope'; }", "paths": []})
    assert resp["ok"] is False
    assert "array" in resp["error"]


def test_missing_filter_function_is_error():
    resp = _run({"code": "const x = 1;", "paths": []})
    assert resp["ok"] is False
    assert "filterPaths" in resp["error"]


def test_filter_runtime_error_is_reported():
    resp = _run({"code": "function filterPaths(paths){ throw new Error('boom'); }", "paths": []})
    assert resp["ok"] is False
    assert "boom" in resp["error"]


def test_invalid_json_request_is_error():
    resp = _run("this is not json")
    assert resp["ok"] is False
    assert "invalid request" in resp["error"]


def test_request_without_code_is_error():
    resp = _run({"paths": ["/data/a"]})
    assert resp["ok"] is False
    assert "code" in resp["error"]
