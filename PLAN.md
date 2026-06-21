# Plan: extract a reusable `Sandbox` component

Status: **proposed, not yet implemented.**

Goal: isolate pfind's hardened-Docker execution into a small, domain-agnostic
`Sandbox` component, behind an interface, so that (a) the security-relevant flag set
lives in one auditable place, (b) tests can run without Docker via a fake, and (c) an
alternate backend (Podman, gVisor, rootless) could slot in later without touching pfind
domain logic.

This is **Phase A** of the altitude options discussed (in-repo module behind a
`Protocol`). Phase B (multi-backend) falls out almost for free; Phase C (separate PyPI
package) is explicitly deferred until a second consumer exists.

---

## 1. Boundary: what moves vs. what stays

**Moves into the new component (generic, knows nothing about prompts/paths/filters):**

- The hardened `docker run` invocation currently inlined in `run_filter`
  (`backend.py`): `--network none`, `--read-only`, `--cap-drop ALL`,
  `--security-opt no-new-privileges`, `--pids-limit`, `--memory`, `--cpus`,
  `--ulimit nofile`, `--tmpfs /tmp`, container naming, `--rm`, `--interactive`.
- Docker mechanics: `_run_docker`, `check_docker_available`, `build_image`,
  `_image_exists`, `_derived_image_tag`, `_dockerfile_path`, `_docker_error_detail`,
  `_remove_container`.
- Timeout-kill handling, output-size cap enforcement.
- "Layer a Dockerfile on top of a base image and return a content-hashed tag" — the
  generic half of `build_worker_image`.

**Stays in pfind (domain contract with *its* worker — must NOT leak into the component):**

- The `/data` mount convention and the `paths`/`meta` request payload shape.
- The JSON protocol `{code, paths, meta}` → `{ok, results|error}` (`worker.py`).
- `_normalize_results` (results must be a subset of the input paths).
- macOS `META` injection.
- `Runtime` image recipes, dependency derivation, and the whitelist gating.

---

## 2. Target API (`src/pfind/sandbox.py`)

```python
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Mount:
    source: Path
    target: str
    read_only: bool = True


@dataclass(frozen=True)
class Limits:
    memory: str = "256m"
    cpus: float = 1.0
    pids: int = 64
    timeout: float = 10.0
    max_output_bytes: int = 1_000_000


@dataclass(frozen=True)
class CompletedRun:
    stdout: bytes
    stderr: bytes
    returncode: int


class SandboxError(RuntimeError): ...
class SandboxUnavailable(SandboxError): ...        # daemon/CLI missing (maps DockerUnavailableError)
class SandboxTimeout(SandboxError): ...            # run exceeded Limits.timeout
class SandboxOutputTooLarge(SandboxError): ...     # stdout/stderr exceeded max_output_bytes


class Sandbox(Protocol):
    def ensure_image(self, *, rebuild: bool = False) -> None: ...
    def derive_image(self, dockerfile_text: str) -> str: ...   # returns the runnable tag
    def run(self, stdin: bytes, *, mounts: list[Mount], limits: Limits) -> CompletedRun: ...


class DockerSandbox:
    """Concrete Sandbox backed by the `docker` CLI. Owns the hardened flag set."""
    def __init__(self, image: str, *, dockerfile: Path, build_timeout: float = 120.0,
                 name_prefix: str = "pfind-search-") -> None: ...
    # implements ensure_image / derive_image / run
```

Notes:
- `run` raises `SandboxTimeout` / `SandboxOutputTooLarge` / `SandboxUnavailable`; it does
  **not** interpret exit codes or parse stdout — that's the caller's job.
- `derive_image` keeps the content-hash tagging (`<repo>:deps-<sha256[:12]>`) so derived
  images are cached and reused; the hash input is the Dockerfile text.
- The hardened flags are assembled in exactly one private method for auditability.

---

## 3. pfind becomes a thin adapter

`backend.run_filter` collapses to: build request → `sandbox.run(...)` → validate.

```python
def run_filter(code, search_root, container_paths, *, sandbox, meta=None, limits=Limits()):
    root = search_root.expanduser().resolve(strict=True)
    request = json.dumps({"code": code, "paths": container_paths, "meta": meta or {}}).encode()
    run = sandbox.run(request, mounts=[Mount(root, "/data", read_only=True)], limits=limits)
    if run.returncode != 0:
        raise RuntimeError(f"Docker worker failed: {run.stderr.decode(errors='replace').strip() or run.returncode}")
    response = _parse_worker_response(run.stdout)        # JSON + ok/error checks (pfind-specific)
    return _normalize_results(response.get("results"), set(container_paths))
```

`build_worker_image` keeps the `Runtime`/whitelist logic but delegates the actual build to
`sandbox.derive_image(runtime.derived_dockerfile(base, packages))`.

`_run_generated` / `search` / `run_saved` construct (or receive) a `DockerSandbox` for the
chosen runtime and pass it down. Decide: thread a `sandbox` parameter through, or build it
inside `_run_generated` from the `Runtime` (default) with an optional override for tests.
**Recommended:** optional `sandbox: Sandbox | None = None` on `search`/`run_saved`,
defaulting to a `DockerSandbox` built from the runtime — mirrors the existing optional
`whitelist` parameter and keeps the public API backward compatible.

---

## 4. Test strategy — `FakeSandbox`

Add a `FakeSandbox` (test helper) implementing the `Sandbox` Protocol:
- `run()` returns a canned `CompletedRun` (or runs `_worker_response` in-process to
  exercise the real filter logic without Docker), and can be configured to raise
  `SandboxTimeout` / `SandboxOutputTooLarge`.
- `ensure_image` / `derive_image` record calls and no-op.

Migration:
- Replace `patch.object(MODULE, "_run_docker", ...)`-style tests that exercise
  `run_filter`/`search`/`run_saved` with a `FakeSandbox` injected via the new parameter.
- Keep a **small** set of real-Docker integration tests (build image + one end-to-end
  search), marked so they can be skipped where Docker is absent.
- The pure Docker-mechanics tests (`build_image` daemon-unavailable, timeout mapping,
  etc.) move to `test_sandbox.py` and target `DockerSandbox` directly, still patching
  `_run_docker` at that layer.

Net effect: most of the suite no longer needs Docker; the security flag set gets its own
focused assertions in `test_sandbox.py` (assert the exact `docker run` argv).

---

## 5. Backward-compatibility & re-exports

- Keep `backend.py` re-exporting the names tests/CLI currently use
  (`run_filter`, `build_worker_image`, `check_docker_available`, `DockerError`,
  `DockerUnavailableError`, ...). Map `SandboxUnavailable` ↔ `DockerUnavailableError`
  (either subclass one from the other or alias) so existing `except DockerUnavailableError`
  call sites and tests keep working.
- `docs/api.md` lists `build_image`, `build_worker_image`, `run_filter`,
  `check_docker_available` as building blocks — keep them working (they may become thin
  wrappers over the sandbox). Update the doc if signatures change (e.g. the new optional
  `sandbox`/`limits` params).

---

## 6. Step-by-step (suggested order, each step green)

1. Add `src/pfind/sandbox.py` with the dataclasses, `Sandbox` Protocol, error types, and
   `DockerSandbox` — moving `_run_docker`, `check_docker_available`, `build_image`,
   `_image_exists`, `_derived_image_tag`, `_dockerfile_path`, `_docker_error_detail`,
   `_remove_container`, and the hardened `docker run` assembly into it.
2. Re-export the moved names from `backend.py`; wire `DockerError`/`DockerUnavailableError`
   to the new error hierarchy. Run the suite — should pass unchanged.
3. Refactor `run_filter` into the adapter form; introduce `_parse_worker_response`.
4. Refactor `build_worker_image` to delegate building to `sandbox.derive_image`.
5. Thread an optional `sandbox` parameter through `_run_generated` / `search` / `run_saved`.
6. Add `FakeSandbox`; migrate the Docker-mocking tests to it; split pure-mechanics tests
   into `test_sandbox.py`.
7. Update `docs/api.md` and add a `CHANGELOG.md` "Changed" entry.

---

## 7. Out of scope (deferred)

- **Phase C**: extracting `sandbox.py` into its own PyPI package — only when a second
  consumer exists.
- **Alternate backends** (Podman/gVisor/Firecracker/rootless) — the Protocol makes these
  possible; implement on demand.
- **Abstracting the JSON stdin/stdout protocol or the worker-supervisor pattern** —
  intentionally left as a pfind concern (thinner, domain-shaped seam; risk of leaking
  domain into the generic component).

---

## 8. Risks / watch-outs

- **The container ships `worker.py` standalone** (`Dockerfile.python` copies it, runs
  `python worker.py --worker`). The sandbox extraction is host-side only and must not
  disturb this — `worker.py` stays import-free and standalone. (This already bit us once
  during the module split.)
- **Patching seams change**: tests that patch `MODULE._run_docker` must move to patching
  it on the sandbox module, or switch to `FakeSandbox`. Inventory these before step 6.
- **Error-type identity**: any code doing `except DockerUnavailableError` (including the
  CLI) must keep working — verify the alias/subclass mapping with a test.
- **`MAX_RESULT_BYTES`** currently lives in `worker.py` (canonical) and is imported by
  `run_filter`. In the new design it becomes `Limits.max_output_bytes`; keep `worker.py`'s
  own copy for the standalone container path and pass the limit in from the host side.
