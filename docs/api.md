# Python API

← [Home](index.md)

> **Note:** The programmatic API is still evolving and may change between releases
> without notice. Pin a specific version if you depend on it. The CLI interface is
> stable.

The public entry point is `search`, re-exported from the top-level package:

```python
from pfind import search

# Returns a list of records, each a dict with at least a "path" key (a host path).
# When the prompt asks for extra per-file values, they appear as additional keys.
records = search(".", "directories that contain only audio files")

for record in records:
    print(record["path"])
```

Requirements are the same as the CLI: Docker running, and `OPENAI_API_KEY` set in the
environment. The first call builds the worker image; later calls reuse it.

## `search`

```python
def search(
    path: str,
    prompt: str,
    *,
    image: str | None = None,         # override the chosen runtime's base tag
    model: str = "gpt-4o-mini",
    timeout: float = 10.0,
    memory: str = "256m",
    cpus: float = 1.0,
    pids_limit: int = 64,
    rebuild: bool = False,
    build_timeout: float = 120.0,
    on_generated: Callable[[GeneratedFilter], None] | None = None,
    on_retry: Callable[[int, ValueError], None] | None = None,
    approve_dependencies: Callable[[list[str]], bool] | None = None,
    whitelist: set[str] | None = None,
    macos_meta: bool = False,            # macOS: expose tags/quarantine to the filter
    sandbox: Sandbox | None = None,      # override the execution backend (see below)
) -> list[dict[str, Any]]:
```

Generates a filter for `prompt`, runs it against `path` in the sandbox, and returns
the matching paths as records (host paths plus any extra fields the prompt produced).
The model picks the [runtime](runtimes.md) (Python or Node.js) and the matching base
image is used unless `image` overrides it. The keyword arguments mirror the
[CLI options](cli.md#options).

`model` accepts a bare name (OpenAI) or a `provider/model` selector for any
OpenAI-compatible provider in `backend.PROVIDERS` (`anthropic/…`, `gemini/…`,
`groq/…`, `ollama/…`, `openrouter/<vendor>/<model>`, …). pfind reuses the OpenAI SDK
against the provider's base URL and reads its `*_API_KEY`; see
[Providers](cli.md#providers).

### Reviewing or gating the generated code

`on_generated`, if given, is called with the `GeneratedFilter` **after** it is
produced but **before** it runs. It exposes `.code`, `.runtime` (`"python"` or
`"node"`), and `.dependencies`. Use it to inspect, log, or save the code — or raise to
abort before execution:

```python
from pfind import search
from pfind.backend import GeneratedFilter

def review(generated: GeneratedFilter) -> None:
    print(f"[{generated.runtime}] {generated.dependencies}")
    print(generated.code)
    if "child_process" in generated.code:    # your own policy check
        raise RuntimeError("rejected")

records = search(".", "files with no extension", on_generated=review)
```

This is the same hook the CLI uses to implement
[`--show-code`, `--save`, and `--confirm`](cli.md#reviewing-the-generated-code).

### Saving and replaying filters

`render_saved_filter(generated, prompt, model)` renders a `GeneratedFilter` as a
self-describing, replayable script (a PEP 723 script for the Python runtime, a
commented raw file for Node) — the same artifact the CLI's `--save` writes.
`run_saved(filter_path, path, …)` parses such a file back and replays it through the
sandbox without an LLM call, gating any declared packages through
`approve_dependencies`/the whitelist exactly as `search` does:

```python
from pathlib import Path

from pfind import render_saved_filter, run_saved, search
from pfind.backend import GeneratedFilter

# Capture and persist a filter while searching:
saved: list[GeneratedFilter] = []
search(".", "Python files that import os", on_generated=saved.append)
Path("os-imports.py").write_text(
    render_saved_filter(saved[0], "Python files that import os", "gpt-4o-mini")
)

# Later, replay it sandboxed with no model call:
records = run_saved("os-imports.py", "./src")
```

See [Saving & replaying filters](cli.md#saving--replaying-filters) for the file format
and the `uv run` trusted fast path.

### Generation retries

The model is asked for the filter in a **single** call. If its reply fails validation
(malformed JSON, the wrong function shape, an invalid package name, an unknown
runtime), pfind feeds the error back and retries — up to 3 attempts in total. The
first attempt runs at temperature 0; retries nudge the temperature up so the model
diverges from the reply that just failed. Only validation errors are retried; API,
Docker, and dependency-approval failures are not. If every attempt fails, the last
validation error is raised.

`on_retry`, if given, is called with the 1-based retry number and the `ValueError`
before each retry — handy for logging. The CLI uses it to print a notice under
[`--verbose`](cli.md#options). `generate_filter` takes the same `on_retry`, plus an
`attempts` argument (default 3) to tune or disable retries.

### Approving dependencies

If the generated filter requests third-party packages that aren't already approved,
`approve_dependencies` is called with the new package names. Return `True` to install
them (into a derived image) and remember them; return `False` (the default behaviour
when no approver is given) to reject with a `DependencyError`. `whitelist` overrides
the approved set (defaults to `load_whitelist(runtime)`, i.e. the chosen runtime's
built-in list plus saved approvals). `load_whitelist` and `approve_packages` take a
`runtime` argument (`"python"` or `"node"`, default `"python"`).

```python
from pfind import search, load_whitelist

records = search(
    "~/Music",
    "MP3 files whose title tag contains 'live', using mutagen",
    approve_dependencies=lambda packages: True,   # auto-approve (like --yes)
    whitelist=load_whitelist() | {"tinytag"},
)
```

See [Dependencies & the whitelist](dependencies.md) for the full model.

### macOS metadata

With `macos_meta=True` on a macOS host, pfind reads selected per-path attributes
(Finder tags, quarantine/where-from) during enumeration and exposes them to a Python
filter as a global `META` dict, so filters can combine macOS metadata with file
contents. It is a no-op on other platforms. See
[macOS metadata](macos-metadata.md) for the field schema and examples.

## Errors

```python
from pfind import DependencyError, DockerError, DockerUnavailableError

try:
    records = search(".", "files with no extension")
except DockerUnavailableError as exc:
    ...   # Docker CLI or daemon could not be reached
except DockerError as exc:
    ...   # other Docker lifecycle failures (build/run)
except DependencyError as exc:
    ...   # filter needed packages that were not approved
```

`DockerUnavailableError` is a subclass of `DockerError`. `DependencyError` is raised
when a filter needs unapproved packages. Filter execution problems (timeouts, invalid
results) surface as `TimeoutError` and `RuntimeError`.

## Lower-level building blocks

For finer control, `pfind.backend` exposes the individual steps that `search`
orchestrates:

```python
from pathlib import Path
from pfind import backend

root = Path(".").resolve()
container_paths, host_by_container = backend.enumerate_paths(root)
generated = backend.generate_filter("files with no extension")   # .code and .dependencies
image = backend.build_worker_image(dependencies=generated.dependencies)
records = backend.run_filter(generated.code, root, container_paths, image=image)
```

| Function | Purpose |
|---|---|
| `enumerate_paths(root)` | Walk the tree; return container paths and a container→host map. |
| `collect_macos_metadata(host_by_container)` | macOS: read tags/quarantine/where-from per path; `{}` off macOS. |
| `generate_filter(prompt, model=…, attempts=…, on_retry=…)` | Ask the LLM for a `GeneratedFilter` (`.code` + `.dependencies`), validated for shape; retries on invalid replies. |
| `build_image(image=…, rebuild=…, build_timeout=…)` | Build the stdlib-only base worker image when absent or on request. |
| `build_worker_image(image=…, dependencies=…, …)` | Ensure a runnable image (base, or a derived image with packages); return the tag to run. |
| `run_filter(code, root, container_paths, …)` | Execute the filter in the sandbox; return container-path records. Pass `limits=Limits(…)` to set the resource/output caps directly, or a `sandbox=` to override the backend. |
| `load_whitelist()` / `approve_packages(pkgs)` | Read the approved-package set / persist new approvals. |
| `check_docker_available()` | Raise `DockerUnavailableError` if Docker can't be reached. |

These return container paths (`/data/...`); `search` maps them back to host paths.

## The sandbox component

The hardened Docker execution lives behind a small, domain-agnostic `Sandbox` protocol
in `pfind.sandbox`. The default backend, `DockerSandbox`, owns the security-relevant
`docker run` flag set (no network, read-only root, dropped capabilities,
`no-new-privileges`, and process/memory/CPU/file-descriptor/tmpfs limits) in one
auditable place, plus the image build/derive mechanics. `build_worker_image`,
`run_filter`, `build_image`, and `check_docker_available` are thin adapters over it.

`search` and `run_saved` accept an optional `sandbox` to override the backend — pass a
fake implementing the protocol to drive the pfind logic without Docker, or an alternate
backend later:

```python
from pfind import search
from pfind.sandbox import CompletedRun, Limits, Mount

class DryRunSandbox:                 # structural match for the Sandbox protocol
    def ensure_image(self, *, rebuild: bool = False) -> None: ...
    def derive_image(self, dockerfile_text: str, *, rebuild: bool = False) -> str:
        return "dry-run:latest"
    def run(self, stdin: bytes, *, mounts: list[Mount], limits: Limits) -> CompletedRun:
        return CompletedRun(stdout=b'{"ok": true, "results": []}', stderr=b"", returncode=0)

records = search(".", "files with no extension", sandbox=DryRunSandbox())
```

`run` raises `SandboxTimeout` / `SandboxOutputTooLarge` / `SandboxUnavailable`
(`DockerUnavailableError` is an alias of `SandboxUnavailable`); it does not interpret
exit codes or parse output — `run_filter` does that.
