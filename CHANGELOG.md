# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
  `lmstudio`. pfind reuses the OpenAI SDK against each provider's base URL, so no extra
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
  JSON, wrong function shape, an invalid package name), pfind feeds the error back and
  retries (up to 3 attempts total); `--verbose` reports when a retry happens.

### Changed

- Filter-generation requests tolerate providers without strict JSON mode: pfind drops
  `response_format` on rejection and recovers the JSON object from a fenced or chatty
  reply, then validates as usual.
- The system prompt guides tree-sitter usage toward the modern API and the
  per-language grammar wheels, and auto-adds the `tree-sitter` core when a grammar
  wheel is requested (pip does not pull it in automatically).

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
