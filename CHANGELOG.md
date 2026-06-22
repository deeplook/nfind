# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **find-style enumeration filters.** New options narrow what's searched *before* the
  path list reaches the generated filter (host-side, deterministic, and faster on large
  trees): `--exclude GLOB` (repeatable; matches an entry's name and its root-relative
  path, pruning matching directories), `--max-depth N` (limit traversal depth; a direct
  child is depth 1), and a default ignore set (`.git`, `node_modules`, `__pycache__`,
  `.venv`, common caches, …) that `--no-ignore` disables. All apply to `--run` replays
  and are settable in the [config file](docs/configuration.md#config-file). The
  underlying `enumerate_paths`, `search`, and `run_saved` gained matching keyword
  arguments.
- **`--print0` / `-0`.** Separate results with NUL bytes instead of newlines (the
  `find -print0` / `xargs -0` convention) so paths containing spaces or newlines survive
  shell pipelines. Mutually exclusive with `--json`/`--verbose`.

- **Config file (`--config`).** An optional TOML file can supply defaults for the
  most-used options (`model`, `image`, `timeout`, `memory`, `cpus`, `pids-limit`,
  `build-timeout`, `json`, `verbose`, `no-format`), so you don't repeat them on every
  run. nfind reads `--config PATH`, then `$NFIND_CONFIG`, then
  `$XDG_CONFIG_HOME/nfind/config.toml` (used only if present); command-line options
  always win over the file, which wins over the built-in defaults. Per-invocation actions
  (`--save`/`--run`) and package-approval shortcuts (`--yes`/`--no-deps`) are
  intentionally not configurable. Unknown keys and wrong value types are reported with
  the offending key. See [Configuration](docs/configuration.md#config-file).

- **Kotlin, Swift, and Dart parsing.** Added `tree-sitter-kotlin`, `tree-sitter-swift`,
  and `tree-sitter-dart` to the Python default whitelist. Like the other grammar wheels,
  each bundles its compiled grammar, so a filter can parse these languages' structure
  offline in the sandbox (the same per-wheel approach — not `tree-sitter-language-pack`,
  which fetches grammars from the network at runtime). Verified end-to-end in Docker:
  Kotlin/Swift/Dart and TypeScript/TSX all parse cleanly inside the no-network sandbox.
  The system prompt now documents `tree_sitter_typescript`'s two-grammar API
  (`language_typescript()` / `language_tsx()`, not a plain `language()`), so generated
  TS/TSX filters select the right grammar.
- **Ruff cleanup of generated filters.** Generated Python filters are tidied with ruff
  before they are shown, saved, or run — unused imports removed, imports sorted, and the
  source reformatted at a pinned line length of 100. The transforms preserve behaviour
  (and fall back to the original on any failure), so the reviewed/saved/run code is
  identical. `--no-format` skips the pass; ruff is now a runtime dependency.
- **Saveable, replayable filters.** `--save PATH` now writes the generated filter as a
  self-describing [PEP 723](https://peps.python.org/pep-0723/) script (Python runtime):
  a `# /// script` block declaring its dependencies, a module docstring carrying the
  original prompt, provenance and a safety warning, and a `__main__` harness so it runs
  directly via `uv run FILE [PATH]`. The new `--run PATH` replays a saved filter through
  the sandbox with no LLM call (dependencies still gated by the whitelist). Node filters
  are saved with a provenance/safety comment header plus the raw code (standalone
  `uv run` is Python-only; they still replay with `--run`). `render_saved_filter()` and
  `run_saved()` are exposed from the Python API. See
  [Saving & replaying filters](docs/cli.md#saving--replaying-filters).
- **Multi-provider model selection.** `--model` now accepts a `provider/model` selector
  to use any OpenAI-compatible provider — `openai` (default, bare names), `anthropic`,
  `gemini`, `groq`, `mistral`, `deepseek`, `xai`, `openrouter`, and local `ollama` /
  `lmstudio`. nfind reuses the OpenAI SDK against each provider's base URL, so no extra
  dependency is needed; each provider reads its own `*_API_KEY`. See
  [Providers](docs/cli.md#providers).
- **macOS metadata (`--macos-meta`).** On macOS, exposes Finder tags and download
  provenance (quarantine flag and where-from URLs) to a Python filter via a global
  `META` dict, enabling queries that combine macOS metadata with file contents. Read
  host-side and passed into the sandbox; a no-op off macOS. See
  [macOS metadata](docs/macos-metadata.md).
- **Cross-language source parsing.** `tree-sitter` plus per-language grammar wheels
  (`tree-sitter-python`, `-javascript`, `-typescript`, `-go`, `-rust`, `-java`, `-c`,
  `-bash`) are pre-approved in the Python defaults, so filters can query source
  structure (functions, imports, classes) across many languages without a dedicated
  runtime. The grammars are bundled in their wheels, so parsing works in the
  no-network, read-only sandbox.
- **Automatic generation retries.** When the model's reply fails validation (malformed
  JSON, wrong function shape, an invalid package name), nfind feeds the error back and
  retries (up to 3 attempts total); `--verbose` reports when a retry happens.

### Changed

- **Internal: extracted a reusable `Sandbox` component.** The hardened Docker execution
  now lives behind a small, domain-agnostic `Sandbox` protocol in `nfind.sandbox`, with
  `DockerSandbox` as the default backend. The security-relevant `docker run` flag set
  (no network, read-only root, dropped capabilities, `no-new-privileges`, resource
  limits) is assembled in one auditable place, and the Docker mechanics (`_run_docker`,
  `build_image`, `check_docker_available`, image derive/cache) moved there too;
  `build_worker_image` and `run_filter` are now thin adapters over it. `search` and
  `run_saved` gained an optional `sandbox` parameter so callers (and the test suite) can
  run the nfind logic without Docker or swap in an alternate backend later; `run_filter`
  also accepts a `limits=Limits(…)` to set the resource/output caps directly. The Docker
  error hierarchy is now the `SandboxError` family; `DockerError`/`DockerUnavailableError`
  remain as aliases, so existing `except` call sites and the public API are unchanged.
  A skip-guarded `integration` test suite exercises the real `docker build`/`docker run`
  path (hardened flags, no network, worker protocol, timeout-kill) end to end.
- Filter-generation requests tolerate providers without strict JSON mode: nfind drops
  `response_format` on rejection and recovers the JSON object from a fenced or chatty
  reply, then validates as usual.
- The system prompt guides tree-sitter usage toward the modern API and the
  per-language grammar wheels, and auto-adds the `tree-sitter` core when a grammar
  wheel is requested (pip does not pull it in automatically).
- **Internal: `backend.py` split into focused modules.** The monolithic backend was
  broken into `errors`, `_constants`, `runtimes`, `metadata`, `whitelist`, and `saved`
  modules; `backend` keeps generation, Docker, and orchestration and re-exports the
  moved names, so the public Python API (`nfind.search`, `run_saved`, etc.) is
  unchanged. The in-container worker now lives in a standalone, standard-library-only
  `worker.py` that the Docker image ships and runs (`python worker.py --worker`),
  mirroring the existing `worker_node.cjs` design; `Dockerfile.python` copies `worker.py`
  instead of `backend.py`.

## [0.1.0]

### Added

- Initial release: natural-language file search that generates a filter with an LLM and
  runs it in a hardened, disposable Docker container (read-only mount, no network,
  dropped capabilities, resource limits).
- Python and Node.js runtimes, chosen per prompt by the model.
- Declared dependencies gated by a per-runtime whitelist, installed into cached derived
  images.
- Output modes (paths, `--verbose`, `--json`), code review (`--show-code`, `--save`,
  `--confirm`), and a `search()` Python API.
