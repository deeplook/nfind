# Python API

← [Home](index.md)

> **Note:** The programmatic API is still evolving and may change between releases
> without notice. Pin a specific version if you depend on it. The CLI interface is
> stable.

The public entry point is `search`, re-exported from the top-level package:

```python
from nfind import search

# Returns a list of records, each a dict with at least a "path" key (a host path).
# When the prompt asks for extra per-file values, they appear as additional keys.
records = search(".", "directories that contain only audio files")

for record in records:
    print(record["path"])
```

Requirements are the same as the CLI: Docker running by default (or an experimental
alternate backend — Apple Containers on macOS via `sandbox_backend="apple"`, Podman via
`sandbox_backend="podman"`, or nerdctl/containerd via `sandbox_backend="nerdctl"`), and
`OPENAI_API_KEY` set in the environment. The first call builds the worker image; later
calls reuse it. The Apple backend is experimental on macOS 15 because it cannot disable
networking the way Docker does; the Podman backend is experimental because it has been
validated only on limited hosts and rootless Podman's isolation differs from a rootful
Docker daemon (nfind remaps the read-only mount to the worker user via `--userns=keep-id`
so rootless runs stay readable); the nerdctl backend is experimental — validated on Linux CI
against rootful containerd, with rootless containerd unsupported (no `keep-id` remap).

## `search`

```python
def search(
    path: str | Path | Sequence[str | Path],
    prompt: str,
    *,
    image: str | None = None,         # override the chosen runtime's base tag
    model: str = "openai/gpt-5.4",
    timeout: float = 180.0,
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
    format_code: bool = True,            # tidy generated Python with ruff before running
    sandbox: Sandbox | None = None,      # override the execution backend (see below)
    sandbox_backend: Literal["docker", "apple", "podman", "nerdctl"] = "docker",
    exclude: Sequence[str] = (),         # glob patterns to prune before filtering
    max_depth: int | None = None,
    use_default_ignores: bool = True,
) -> list[dict[str, Any]]:
```

Generates a filter for `prompt`, runs it against `path` in the sandbox, and returns
the matching paths as records (host paths plus any extra fields the prompt produced).
`path` may be one root or a sequence of roots, each a directory (walked) or a single
file; with several roots each is mounted separately and results are merged as host
paths. The model picks the
[runtime](runtimes.md) (Python or Node.js) and the matching base image is used unless
`image` overrides it. The keyword arguments mirror the [CLI options](cli.md#options):
`exclude`, `max_depth`, and `use_default_ignores` shape host-side enumeration before
the generated filter runs, `format_code=False` matches `--no-format`, and
`sandbox_backend="apple"` matches `--sandbox apple`.

When using `sandbox_backend="apple"` on macOS 15, apply the same caveat as the CLI:
Apple's `container` does not support Docker's `--network none` there. nfind uses
`--no-dns`, but raw IP network access may still be possible.

`model` accepts a bare name (OpenAI) or a `provider/model` selector for any
OpenAI-compatible provider in `backend.PROVIDERS` (`anthropic/…`, `gemini/…`,
`groq/…`, `ollama/…`, `openrouter/<vendor>/<model>`, …). nfind reuses the OpenAI SDK
against the provider's base URL and reads its `*_API_KEY`; see
[Providers](cli.md#providers).

### Reviewing or gating the generated code

`on_generated`, if given, is called with the `GeneratedFilter` **after** it is
produced but **before** it runs. It exposes `.code`, `.runtime` (`"python"` or
`"node"`), and `.dependencies`. Use it to inspect, log, or save the code — or raise to
abort before execution:

```python
from nfind import search
from nfind.backend import GeneratedFilter

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

`serialize_filter(generated, prompt, model)` renders a `GeneratedFilter` as a
self-describing, replayable script (a PEP 723 script for the Python runtime, a
commented file with a machine-readable metadata line for Node) — the same artifact the
CLI's `--save` writes.
`run_saved(filter_path, path, …)` parses such a file back and replays it through the
sandbox without an LLM call, gating any declared Python or Node packages through
`approve_dependencies`/the per-runtime whitelist exactly as `search` does. It accepts
the same one-or-many `path`, `exclude`, `max_depth`, and `use_default_ignores`
enumeration controls as `search`:

```python
from pathlib import Path

from nfind import serialize_filter, run_saved, search
from nfind.backend import GeneratedFilter

# Capture and persist a filter while searching:
saved: list[GeneratedFilter] = []
search(".", "Python files that import os", on_generated=saved.append)
Path("os-imports.py").write_text(
    serialize_filter(saved[0], "Python files that import os", "openai/gpt-5.4")
)

# Later, replay it sandboxed with no model call:
records = run_saved("os-imports.py", "./src")
```

See [Saving & replaying filters](cli.md#saving--replaying-filters) for the file format
and the Python-only `uv run` trusted fast path.

### Generation retries

The model is asked for the filter in a **single** call. If its reply fails validation
(malformed JSON, the wrong function shape, an invalid package name, an unknown
runtime), nfind feeds the error back and retries — up to 3 attempts in total. The
first attempt runs at temperature 0; retries nudge the temperature up so the model
diverges from the reply that just failed. Only validation errors are retried; API,
sandbox backend, and dependency-approval failures are not. If every attempt fails, the
last validation error is raised.

`on_retry`, if given, is called with the 1-based retry number and the `ValueError`
before each retry — handy for logging. The CLI uses it to print a notice under
[`--fields`](cli.md#options). `generate_filter` takes the same `on_retry`, plus an
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
from nfind import search, load_whitelist

records = search(
    "~/Music",
    "MP3 files whose title tag contains 'live', using mutagen",
    approve_dependencies=lambda packages: True,   # auto-approve (like --yes)
    whitelist=load_whitelist() | {"tinytag"},
)
```

See [Dependencies & the whitelist](dependencies.md) for the full model.

### macOS metadata

With `macos_meta=True` on a macOS host, nfind reads selected per-path attributes
(Finder tags, quarantine/where-from) during enumeration and exposes them to a Python
filter as a global `META` dict, so filters can combine macOS metadata with file
contents. It is a no-op on other platforms. See
[macOS metadata](macos-metadata.md) for the field schema and examples.

## Errors

```python
from nfind import DependencyError, DockerError, DockerUnavailableError

try:
    records = search(".", "files with no extension")
except DockerUnavailableError as exc:
    ...   # selected sandbox CLI/daemon/services could not be reached
except DockerError as exc:
    ...   # other sandbox lifecycle failures (build/run)
except DependencyError as exc:
    ...   # filter needed packages that were not approved
```

`DockerUnavailableError` is a backwards-compatible alias for the generic sandbox
unavailable error, and remains a subclass of `DockerError`. `DependencyError` is raised
when a filter needs unapproved packages. Filter execution problems (timeouts, invalid
results) surface as `TimeoutError` and `RuntimeError`.

## Lower-level building blocks

For finer control, `nfind.generation` owns the model-to-filter step,
`nfind.enumeration` owns host-side path enumeration, and `nfind.execution` owns
sandbox image preparation, dependency gating, and worker execution. `nfind.backend`
orchestrates those pieces and re-exports the older helper names for compatibility:

```python
from pathlib import Path
from nfind import enumeration, execution, generation, runtimes

root = Path(".").resolve()
container_paths, host_by_container, mounts = enumeration.enumerate_roots([root])
generated = generation.generate_filter("files with no extension")   # .code and .dependencies
runtime = runtimes.RUNTIMES[generated.runtime]
image = execution.build_worker_image(
    runtime.base_image,
    generated.dependencies,
    runtime=runtime,
)
records = execution.run_filter(
    generated.code,
    root,
    container_paths,
    image=image,
    mounts=mounts,
)
```

| Function | Purpose |
|---|---|
| `enumeration.enumerate_paths(root, exclude=…, max_depth=…, use_default_ignores=…)` | Walk one tree; return container paths and a container→host map. `exclude` prunes matching globs, `max_depth` bounds depth, and default VCS/dependency/cache dirs are skipped unless disabled. Also re-exported from `nfind.backend` for compatibility. |
| `enumeration.enumerate_roots(roots, exclude=…, max_depth=…, use_default_ignores=…)` | Walk one or more roots; return container paths, a container→host map, and the mounts needed for execution. This is what `search` and `run_saved` use. |
| `collect_macos_metadata(host_by_container)` | macOS: read tags/quarantine/where-from per path; `{}` off macOS. |
| `generation.generate_filter(prompt, model=…, attempts=…, on_retry=…)` | Ask the LLM for a `GeneratedFilter` (`.code` + `.dependencies`), validated for shape; retries on invalid replies. Also re-exported from `nfind.backend` for compatibility. |
| `build_image(image=…, rebuild=…, build_timeout=…)` | Build the stdlib-only base worker image when absent or on request. |
| `execution.build_worker_image(image=…, dependencies=…, …)` | Ensure a runnable image (base, or a derived image with packages); return the tag to run. Also re-exported from `nfind.backend` for compatibility. |
| `execution.run_filter(code, root, container_paths, …)` | Execute the filter in the sandbox; return container-path records. Pass `limits=Limits(…)` to set the resource/output caps directly, or a `sandbox=` to override the backend. Also re-exported from `nfind.backend` for compatibility. |
| `load_whitelist(runtime="python")` / `approve_packages(pkgs, runtime="python")` | Read the approved-package set / persist new approvals for one runtime (`"python"` or `"node"`). |
| `check_docker_available()` | Raise `DockerUnavailableError` if Docker can't be reached. |
| `check_sandbox_available("docker" | "apple" | "podman" | "nerdctl")` | Raise `DockerUnavailableError` if the selected sandbox backend can't be reached. |

These lower-level helpers return the in-container paths the filter will see — each root's
own host path when it can be safely mounted there, or neutral `/data` / `/data/0` … mount
points otherwise — alongside the container→host map that `search` and `run_saved` use to
translate results back.

## The sandbox component

The hardened execution lives behind a small, domain-agnostic `Sandbox` protocol in
`nfind.sandbox`. The default backend, `DockerSandbox`, owns the security-relevant
`docker run` flag set (no network, read-only root, dropped capabilities,
`no-new-privileges`, and process/memory/CPU/file-descriptor/tmpfs limits) in one
auditable place, plus the image build/derive mechanics. `AppleContainerSandbox` is an
experimental macOS backend selected with `sandbox_backend="apple"`; on macOS 15 it
does not provide Docker-equivalent no-network isolation. `PodmanSandbox`
(`sandbox_backend="podman"`) reuses the identical Docker-family run command, adding a
rootless `--userns=keep-id` remap so the non-root worker can read the mount; it stays
experimental because it is validated only on limited hosts. `NerdctlSandbox`
(`sandbox_backend="nerdctl"`) likewise reuses the Docker-family run command for
containerd via the `nerdctl` CLI; it is experimental — validated on Linux CI against
rootful containerd, with no rootless mount remap (nerdctl lacks Podman's `keep-id`).
The backends live in one module each under `nfind.sandbox` (`base`, `docker`, `apple`,
`podman`, `nerdctl`), sharing a `_CliSandbox` base class.
`execution.build_worker_image` and `execution.run_filter` are nfind-specific adapters
over the selected backend.

`search` and `run_saved` accept an optional `sandbox` to override the backend — pass a
fake implementing the protocol to drive the nfind logic without Docker, or an alternate
backend later:

```python
from nfind import search
from nfind.sandbox import CompletedRun, Limits, Mount

class DryRunSandbox:                 # structural match for the Sandbox protocol
    def check_available(self) -> None: ...
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
