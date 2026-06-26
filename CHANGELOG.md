# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Generate-only mode.** Omitting `PATH` generates the filter (LLM call) without
  running the sandbox or enumerating any paths — useful with `--save` to capture a
  filter for later replay, or with `--show-code` to inspect it inline. nfind warns
  when no path and none of `--save`, `--show-code`, or `--confirm` is given, since
  the filter would otherwise be silently discarded. `backend.generate_only()` exposes
  the same behaviour programmatically.
- **Apple Containers sandbox backend.** `--sandbox apple` can run saved and generated
  filters with Apple's `container` CLI as an opt-in alternative to Docker, including
  CLI/config support, resource limits, read-only mounts, and integration coverage.
- **macOS-aware Apple networking.** On macOS 26+ nfind uses Apple Containers'
  `--network none`; on macOS 15 it falls back to `--no-dns` and prints an explicit
  warning about the weaker network isolation.

## [0.1.0] - 2026-06-23

### Added

- **Natural-language file search.** Describe what you are looking for in plain English;
  nfind generates a filter with an LLM and runs it in a hardened, disposable Docker
  container (read-only mount, no network, dropped capabilities, resource limits).
- **Python and Node.js runtimes.** The model picks the right runtime per prompt; declared
  dependencies are gated by a per-runtime whitelist and installed into cached derived
  images.
- **Multiple search roots.** `nfind PROMPT PATH...` accepts more than one directory
  (e.g. `nfind "TODO comments" ./src ./tests`); each root is mounted separately and
  namespaced internally so identically named files in different roots never collide, and
  results are merged into one list.
- **find-style enumeration filters.** `--exclude GLOB` (repeatable), `--max-depth N`, and
  a default ignore set (`.git`, `node_modules`, `__pycache__`, `.venv`, common caches, …)
  that `--no-ignore` disables narrow what is searched before the path list reaches the
  generated filter — host-side, deterministic, and faster on large trees.
- **Config file (`--config`).** An optional TOML file supplies defaults for the most-used
  options (`model`, `image`, `timeout`, `memory`, `cpus`, `pids-limit`, `build-timeout`,
  `json`, `verbose`, `no-format`). nfind reads `--config PATH`, then `$NFIND_CONFIG`,
  then `$XDG_CONFIG_HOME/nfind/config.toml`; command-line options always win. See
  [Configuration](docs/configuration.md#config-file).
- **Multi-provider model selection.** `--model` accepts a `provider/model` selector to
  use any OpenAI-compatible provider — `openai` (default), `anthropic`, `gemini`, `groq`,
  `mistral`, `deepseek`, `xai`, `openrouter`, and local `ollama`/`lmstudio`. Each
  provider reads its own `*_API_KEY`. See [Providers](docs/cli.md#providers).
- **Saveable, replayable filters.** `--save PATH` writes the generated Python filter as a
  self-describing [PEP 723](https://peps.python.org/pep-0723/) script with a `# ///
  script` block, a module docstring carrying the original prompt, and a `__main__` harness
  so it runs directly via `uv run FILE [PATH]`. `--run PATH` replays a saved filter
  through the sandbox with no LLM call. See
  [Saving & replaying filters](docs/cli.md#saving--replaying-filters).
- **Ruff cleanup of generated filters.** Generated Python filters are tidied with ruff
  before being shown, saved, or run — unused imports removed, imports sorted, source
  reformatted at 100 characters. `--no-format` skips the pass.
- **Automatic generation retries.** When the model's reply fails validation (malformed
  JSON, wrong function shape, invalid package name), nfind feeds the error back and
  retries up to 3 attempts; `--verbose` reports each retry.
- **Cross-language source parsing.** `tree-sitter` and per-language grammar wheels
  (`tree-sitter-python`, `-javascript`, `-typescript`, `-go`, `-rust`, `-java`, `-c`,
  `-bash`, `-kotlin`, `-swift`, `-dart`) are pre-approved in the Python defaults, so
  filters can query source structure (functions, imports, classes) across many languages
  inside the no-network, read-only sandbox.
- **macOS metadata (`--macos-meta`).** On macOS, exposes Finder tags and download
  provenance (quarantine flag, where-from URLs) to a Python filter via a global `META`
  dict. Read host-side and passed into the sandbox; a no-op off macOS. See
  [macOS metadata](docs/macos-metadata.md).
- **Output modes.** Plain paths (default), `--verbose` (tab-separated extra fields),
  `--json` (one JSON record per result), and `--print0` / `-0` (NUL-separated, for
  `xargs -0` pipelines). `--json` and `--verbose` are mutually exclusive with `--print0`.
- **Code review options.** `--show-code` prints the generated filter before running;
  `--confirm` shows it and waits for approval; `--save PATH` persists it.
- **Python API.** `nfind.search()` and `nfind.run_saved()` expose the full search and
  replay pipeline for programmatic use.
