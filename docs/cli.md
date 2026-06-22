# CLI Reference

← [Home](index.md)

- [Synopsis](#synopsis)
- [Arguments](#arguments)
- [Options](#options)
- [Filtering what's searched](#filtering-whats-searched)
- [Reviewing the generated code](#reviewing-the-generated-code)
- [Saving & replaying filters](#saving--replaying-filters)
- [Output modes](#output-modes)
- [Dependencies](#dependencies)
- [Exit codes](#exit-codes)

---

## Synopsis

```bash
pfind PROMPT [PATH] [OPTIONS]
pfind --run FILTER [PATH] [OPTIONS]   # replay a saved filter, no PROMPT
```

Search `PATH` for files and directories matching the natural-language `PROMPT` and
print one path per line. Both `-h` and `--help` show usage. With `--run`, a previously
saved filter is replayed instead and `PROMPT` is omitted (see
[Saving & replaying filters](#saving--replaying-filters)).

```bash
pfind "directories that contain only audio files"
pfind "Python files that import requests" ./src
pfind "files larger than 1 MB, with their size" --verbose
```

## Arguments

| Argument | Default | Description |
|---|---|---|
| `PROMPT` | — (required) | Natural-language description of the paths to find. |
| `PATH` | `.` | Directory to search. |

## Options

| Option | Default | Description |
|---|---|---|
| `--config PATH` | XDG default | TOML file of option defaults (env: `PFIND_CONFIG`); command-line options override it. See [Config file](configuration.md#config-file). |
| `--exclude GLOB` | — | Glob of names/paths to skip during enumeration; matching directories are pruned. Repeatable. See [Filtering what's searched](#filtering-whats-searched). |
| `--no-ignore` | off | Don't skip the default ignored directories (`.git`, `node_modules`, `__pycache__`, `.venv`, caches, …). |
| `--max-depth N` | unlimited | Descend at most `N` directory levels below `PATH` (a direct child is `1`). |
| `--model` | `gpt-4o-mini` | Model used to generate the filter. Bare name = OpenAI; `provider/model` for others (see [Providers](#providers)). |
| `--list-models` | off | List the model ids available for the provider in `--model` and exit. Needs that provider's API key. See [Providers](#providers). |
| `--image` | per-runtime | Override the base image tag for the chosen [runtime](runtimes.md). |
| `--timeout` | `10.0` | Seconds the generated filter may run before it is killed. |
| `--memory` | `256m` | Memory limit for the worker container. |
| `--cpus` | `1.0` | CPU limit for the worker container. |
| `--pids-limit` | `64` | Maximum number of processes inside the worker container. |
| `--rebuild` | off | Rebuild the worker image before searching. |
| `--build-timeout` | `120.0` | Seconds allowed for building the worker image. |
| `--show-code` | off | Print the generated filter (to stderr) before running it. |
| `--save PATH` | — | Save the generated filter as a self-describing, replayable script (see [Saving & replaying filters](#saving--replaying-filters)). |
| `--run PATH` | — | Replay a previously saved filter through the sandbox instead of generating one. No `PROMPT`, no LLM call. |
| `--confirm`, `-i` | off | Show the generated code and ask for confirmation before running it. |
| `--verbose`, `-v` | off | Show extra per-path fields alongside each path. |
| `--json` | off | Output results as JSON (path plus any extra fields). |
| `--print0`, `-0` | off | Separate results with NUL bytes instead of newlines (for `xargs -0`). |
| `--yes`, `-y` | off | Approve any requested packages without prompting. |
| `--no-deps` | off | Reject any third-party packages (standard library only). |
| `--no-format` | off | Skip the ruff cleanup (remove unused imports, sort imports, format) applied to the generated filter. |
| `--macos-meta` | off | macOS only: expose Finder tags and download metadata to the filter (see [macOS metadata](macos-metadata.md)). |
| `-h`, `--help` | — | Show help and exit. |

## Reviewing the generated code

The filter is written by an LLM, so you may want to see it before it runs:

```bash
# Print the generated filter (to stderr) before running it
pfind "files with no extension" --show-code

# Save the generated filter to a file
pfind "files with no extension" --save filter.py

# Show the code and ask for confirmation before running (aborts on "no")
pfind "files with no extension" -i        # or --confirm
```

`--show-code` and `--confirm` print the **full script as [`--save`](#saving--replaying-filters)
would write it** — the PEP 723 metadata, the prompt/provenance docstring, the
`filter_paths` function, and the run harness — so the preview matches the saved
artifact exactly. (On a `--run` replay the saved file is shown as-is.)

Before it is shown, saved, or run, the generated Python filter is tidied with **ruff**:
unused imports are removed, imports are sorted, and the source is reformatted. These
transforms preserve behaviour, so what you review is exactly what runs. Pass
[`--no-format`](#options) to see the model's raw output instead (useful when debugging a
filter the model got wrong).

The code is printed to **stderr**, so stdout stays a clean, pipeable list of paths
even with `--show-code`. On a terminal the code is syntax-highlighted with Pygments;
highlighting is disabled when [`NO_COLOR`](https://no-color.org/) is set or when
stderr is redirected.

Declining a `--confirm` prompt aborts before the container runs and exits with code
130.

## Saving & replaying filters

`--save PATH` writes the generated filter as a **self-describing, replayable script**
rather than a bare function. For the Python runtime that's a
[PEP 723](https://peps.python.org/pep-0723/) script:

```bash
pfind "MP3 files whose title tag contains 'live', using mutagen" ~/Music --save mp3-live.py
```

```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["mutagen"]
# ///
"""
pfind filter

Prompt:  MP3 files whose title tag contains 'live', using mutagen
Model:   gpt-4o-mini
Runtime: python
Saved:   2026-06-21

WARNING: running this file directly (e.g. `uv run`) executes OUTSIDE the pfind
Docker sandbox -- no read-only mount, no network block, full user privileges...
"""

def filter_paths(paths):
    ...

if __name__ == "__main__":
    ...   # walks sys.argv[1] (default ".") and prints matching paths
```

The module docstring carries the original prompt and provenance; the `# /// script`
block declares the filter's dependencies. You can then run it **two ways**:

```bash
# Sandboxed replay through pfind — no LLM call, runs in the same hardened container
pfind --run mp3-live.py ~/Music

# Trusted fast path — runs directly via uv, OUTSIDE the sandbox (see warning below)
uv run mp3-live.py ~/Music
```

`--run` reuses the dependency [whitelist](dependencies.md): a saved filter that
declares a not-yet-approved package still prompts (or is rejected with `--no-deps`),
so a replayed filter can't silently pull new packages.

> **Safety:** `uv run` executes the filter with your full user privileges, network
> access, and write access — none of pfind's [sandbox](safety.md) protections apply.
> Only run files you have reviewed and trust. When in doubt, replay with `pfind --run`,
> which keeps the read-only mount, network block, and resource limits.

Notes and limits:

- `--run` takes no `PROMPT` and ignores `--model`; it can't be combined with `--save`,
  `--confirm`, or `--macos-meta` (using them together exits with code 2).
- `--macos-meta` is **not** available on the replay path — `META` is collected on the
  host during generation and isn't reconstructed for saved filters.
- **Node.js** filters are saved with a `//` provenance/safety comment header plus the
  raw `filterPaths` code. There's no PEP 723 equivalent for Node, so the standalone
  `uv run` path is Python-only; Node filters still replay with `pfind --run`.

## Filtering what's searched

These options shape the path list **before** it reaches the model's filter — they run
on the host during enumeration, so they're deterministic and also make searches faster
by shrinking what the sandbox has to consider.

```bash
pfind "stale config files" ~/project --exclude '*.min.js' --exclude dist
pfind "large modules" ./src --max-depth 2          # only two levels below ./src
pfind "anything referencing the old API" . --no-ignore   # include .git, node_modules, …
```

- **`--exclude GLOB`** — repeatable. Each glob is matched against every entry's name
  *and* its path relative to `PATH` (POSIX form), so `--exclude build` prunes any
  directory named `build`, while `--exclude 'src/generated/*'` targets one location. A
  matching directory is pruned entirely (its subtree is never enumerated).
- **Default ignores** — `.git`, `.hg`, `.svn`, `node_modules`, `.venv`, `venv`,
  `__pycache__`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.tox`, and `.DS_Store`
  are skipped automatically. Pass **`--no-ignore`** to search them too.
- **`--max-depth N`** — descend at most `N` levels below `PATH`; a direct child is depth
  `1`. `N` must be ≥ 1.

All three apply to `--run` replays as well, and can be set as
[config-file defaults](configuration.md#config-file).

## Output modes

```bash
pfind "Python files that import os"                          # default: paths only
pfind "Python files, and for each the number of lines" -v   # path + extra fields
pfind "Python files, and for each the number of lines" --json
pfind "empty directories" ~/Downloads --print0 | xargs -0 rmdir   # NUL-separated
```

`--json` and `--verbose` are mutually exclusive, and `--print0` cannot be combined with
either. `--print0` NUL-terminates each path (the `find -print0` / `xargs -0` convention)
so paths with spaces or newlines survive a pipeline. See [Output modes](output-modes.md)
for details and example output.

## Dependencies

When a prompt needs a library (e.g. reading MP3 tags), the generated filter declares
the PyPI packages it imports. Packages on the approved list install without a prompt;
new ones are confirmed and then remembered:

```bash
pfind "MP3 files whose title tag contains 'live', using mutagen" ~/Music   # prompts if new
pfind "images larger than 4000px on a side" ~/Photos --yes                 # approve without asking
pfind "files containing TODO" . --no-deps                                  # stdlib only
```

`--yes` and `--no-deps` are mutually exclusive. See
[Dependencies & the whitelist](dependencies.md) for the approval flow, the default
package list, and the whitelist file.

## Providers

`--model` selects the model that writes the filter. A bare name uses OpenAI (so
existing usage is unchanged); a `provider/model` selector targets any
OpenAI-compatible provider — pfind reuses the OpenAI SDK against the provider's
base URL, so no extra dependency is needed.

```bash
pfind "files with no extension"                                   # OpenAI (default)
pfind "..." --model anthropic/claude-sonnet-4-6                   # Anthropic
pfind "..." --model gemini/gemini-2.5-flash                       # Google Gemini
pfind "..." --model groq/llama-3.3-70b-versatile                  # Groq
pfind "..." --model openrouter/<vendor>/<model>                   # OpenRouter (near-universal)
pfind "..." --model ollama/llama3.1                               # local Ollama
```

| Provider | Selector prefix | API key env var |
|---|---|---|
| OpenAI | *(bare name)* or `openai/` | `OPENAI_API_KEY` |
| Anthropic | `anthropic/` | `ANTHROPIC_API_KEY` |
| Google Gemini | `gemini/` | `GEMINI_API_KEY` |
| Groq | `groq/` | `GROQ_API_KEY` |
| Mistral | `mistral/` | `MISTRAL_API_KEY` |
| DeepSeek | `deepseek/` | `DEEPSEEK_API_KEY` |
| xAI (Grok) | `xai/` | `XAI_API_KEY` |
| OpenRouter | `openrouter/` | `OPENROUTER_API_KEY` |
| Ollama (local) | `ollama/` | *(none; needs a running server)* |
| LM Studio (local) | `lmstudio/` | *(none; needs a running server)* |

Only the selected provider's key is needed.

### Listing available models

`--list-models` prints the model ids the selected provider exposes, one per line, then
exits. The provider is taken from `--model`, so set it to target a non-default provider:

```bash
pfind --list-models                                    # OpenAI (default provider)
pfind --list-models --model groq/x                     # Groq (model name is ignored here)
pfind --list-models --model openai/x | grep codex      # filter the list
```

Use it to discover valid model names or to check what a local Ollama/LM Studio server
has installed. A provider that doesn't support listing reports an error (exit code 1).

### Endpoint selection (chat completions vs. responses)

pfind speaks two OpenAI-compatible endpoints and picks one per model automatically — no
flag to set:

- **Chat Completions** (`/chat/completions`) is the default and is tried first, so every
  provider above keeps working unchanged.
- **Responses** (`/responses`) is used as an automatic fallback for OpenAI reasoning/codex
  models that are served *only* there (e.g. `gpt-5.1-codex-mini`). When the first request
  is rejected with the tell-tale "only supported in v1/responses" error, pfind switches
  endpoints and retries; the switch is remembered for the rest of that run.

A responses-only model costs one extra throwaway request the first time it's seen (the
probe that triggers the switch). That verdict is then cached on disk — keyed by the full
`provider/model` selector — in `model-endpoints.json` under pfind's cache directory (or
`$PFIND_ENDPOINT_CACHE` when set), so later runs start on `/responses` and skip the probe.
The cache is purely an optimisation: it only ever records the responses-only exceptions,
and every read/write is best-effort, so a missing or stale entry just means one re-probe.

Providers also vary in whether they support strict JSON mode, a custom `temperature`, or
`max_tokens` vs. `max_completion_tokens`; pfind adapts to each rejection automatically and
recovers the JSON from the reply when needed, so generation still works. Some non-OpenAI
models follow the filter contract less reliably — if a model misbehaves, try a stronger
one or route through `openrouter/`.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Search completed (zero or more matches). |
| `1` | A runtime error occurred (e.g. Docker unavailable, filter failed). Message on stderr, prefixed `error:`. |
| `2` | Invalid usage (e.g. `--json` and `--verbose` together). |
| `130` | A `--confirm` prompt was declined. |
