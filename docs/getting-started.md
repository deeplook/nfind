# Tutorial

← [Home](index.md)

A hands-on walkthrough of nfind, from your first search to its most advanced features.
Each section builds on the last; by the end you'll have used every command-line option
at least once.

- [Prerequisites](#prerequisites)
- [1. Your first search](#1-your-first-search)
- [2. Search a specific directory](#2-search-a-specific-directory)
- [3. Read file contents, not just names](#3-read-file-contents-not-just-names)
- [4. Richer output: extra fields and JSON](#4-richer-output-extra-fields-and-json)
- [5. See the code before it runs](#5-see-the-code-before-it-runs)
- [6. Choose a model or provider](#6-choose-a-model-or-provider)
- [7. Filters that need libraries](#7-filters-that-need-libraries)
- [8. Another ecosystem: Node.js / TypeScript](#8-another-ecosystem-nodejs--typescript)
- [9. macOS metadata](#9-macos-metadata)
- [10. Save a filter and replay it](#10-save-a-filter-and-replay-it)
- [11. Tune the sandbox](#11-tune-the-sandbox)
- [12. Rebuild and pin images](#12-rebuild-and-pin-images)
- [13. Set defaults with a config file](#13-set-defaults-with-a-config-file)
- [14. Narrow and speed up big searches](#14-narrow-and-speed-up-big-searches)
- [15. Compose with other tools](#15-compose-with-other-tools)
- [Option cheat sheet](#option-cheat-sheet)

---

## Prerequisites

- **Python 3.11+** and **[Docker](https://docs.docker.com/get-docker/)** installed and
  running (nfind runs every generated filter inside a throwaway container).
- An API key for your model provider. The default is OpenAI:

```bash
uv tool install nfind          # or: pipx install nfind
export OPENAI_API_KEY=sk-...
docker info                    # sanity-check the daemon is up
```

The first real search builds the base worker image (tens of seconds); later runs reuse
it. See [Installation](installation.md) if anything above is missing.

---

## 1. Your first search

The simplest invocation is just a prompt. With no path, nfind searches the current
directory and prints one matching path per line — exactly like `find`:

```bash
nfind "files with no extension"
```

Behind the scenes nfind walks the tree, asks the model for a small Python filter that
matches your description, runs it in the sandbox, and prints the results. The file list
itself never leaves your machine — only your prompt is sent to the model.

---

## 2. Search a specific directory

The second positional argument is the directory to search:

```bash
nfind "directories that contain only audio files" ~/Music
nfind "empty directories (nothing beneath them)" ~/Downloads
nfind "files nested more than four levels deep" ./project
```

Prompts are free-form — describe structure, naming, size, age, or anything the model can
turn into code. See the [Examples gallery](examples.md) for more.

---

## 3. Read file contents, not just names

Because the filter runs *inside* the sandbox with your directory mounted **read-only**,
it can open and inspect files — something `find` alone can't do:

```bash
nfind "Python files that import requests" ./src
nfind "shell scripts that run 'rm -rf'" ~/bin
nfind "JSON files that are not valid JSON"
nfind "Markdown files that contain a TODO but no closing checkbox"
```

The generated code can read bytes, parse structure, and compute — but it cannot write
to your files. The default Docker backend also prevents it from reaching the network;
Apple Containers on macOS 15 has a weaker networking guarantee. See the
[Safety model](safety.md).

---

## 4. Richer output: extra fields and JSON

By default you get plain paths. When your prompt asks for a per-file value, the model
attaches it to each result. Use `--fields` (`-f`) to see those extra fields, or `--json`
for machine-readable records:

```bash
# Default: paths only
nfind "Python files and, for each, its line count"

# --fields / -f: append the extra fields after a tab
nfind "Python files and, for each, its line count" --fields

# --json: a {count, results} object, each result a dict with at least "path"
nfind "the 10 largest log files and their sizes in bytes" --json
```

`--json` and `--fields` are mutually exclusive. More detail in
[Output modes](output-modes.md).

---

## 5. See the code before it runs

nfind generates code, so you may want to inspect or approve it. Three options, from
lightest to strictest:

```bash
# Print the generated filter (syntax-highlighted) before running it
nfind "files that look like secrets or credentials" --show-code

# Pause and ask for confirmation before running (alias: -i)
nfind "files that look like secrets or credentials" --confirm
nfind "files that look like secrets or credentials" -i

# Save the exact filter nfind would run, then decide later (see §10)
nfind "files that look like secrets or credentials" --save scan.py
```

`--confirm` shows the full filter and waits for a yes/no; declining aborts before
anything runs. See [Reviewing the generated code](cli.md#reviewing-the-generated-code).

nfind also tidies generated Python with ruff (removing unused imports, sorting them, and
reformatting) before showing/saving/running it. To skip that pass and see the model's
output verbatim:

```bash
nfind "Python files with unused imports" --show-code --no-format
```

---

## 6. Choose a model or provider

`--model` selects the model. A bare name uses OpenAI; a `provider/model` selector targets
any OpenAI-compatible provider. Each provider reads its own `*_API_KEY`:

```bash
nfind "TypeScript files using generics" --model gpt-4o            # OpenAI (default provider)
nfind "large CSV files" --model anthropic/claude-3-5-sonnet-latest
nfind "duplicate images" --model groq/llama-3.3-70b-versatile
nfind "Go files with TODOs" --model openrouter/google/gemini-2.0-flash-001

# Local servers need no key:
nfind "files modified this week" --model ollama/llama3.1
nfind "files modified this week" --model lmstudio/your-local-model
```

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # only the selected provider's key is needed
```

Not sure what to put after the slash? List a provider's models with `--list-models`:

```bash
nfind --list-models                    # OpenAI (default provider)
nfind --list-models --model groq/x     # another provider (the model name is ignored here)
```

OpenAI reasoning/codex models that are served only on the `/responses` endpoint (e.g.
`gpt-5.1-codex-mini`) work too — nfind detects them and switches endpoints automatically.

See the full [provider table](cli.md#providers) and [endpoint
selection](cli.md#endpoint-selection-chat-completions-vs-responses).

---

## 7. Filters that need libraries

Some questions need a third-party package — reading MP3 tags, image dimensions, PDF text.
The model declares what it needs, and nfind asks before installing anything into a derived
sandbox image:

```bash
nfind "MP3 files whose title tag contains 'live'" ~/Music
#  → The generated filter needs these packages installed in the sandbox: mutagen
#    Install and remember them? [y/N]
```

Approved packages are remembered in a per-runtime **whitelist**, so you're not asked
again. Control the flow with:

```bash
nfind "portrait photos (taller than wide)" ~/Pictures --yes      # approve without prompting (-y)
nfind "PDFs longer than 20 pages" ~/Docs --no-deps               # refuse all third-party packages
```

Common analysis libraries (`mutagen`, `pillow`, `pypdf`, `tree-sitter-*`, …) are
pre-approved out of the box. The whitelist lives at
`$XDG_CONFIG_HOME/nfind/whitelist.json`; relocate it with `NFIND_WHITELIST`. Full model:
[Dependencies & the whitelist](dependencies.md).

```bash
NFIND_WHITELIST=./project-whitelist.json nfind "EXIF GPS-tagged photos" ~/Pictures --yes
```

---

## 8. Another ecosystem: Node.js / TypeScript

The model picks the runtime per prompt. When a task suits the JavaScript/TypeScript
ecosystem (e.g. parsing with `ts-morph`), nfind builds and runs the Node.js sandbox image
automatically — no flag needed:

```bash
nfind "TypeScript files that export a React component" ./src
nfind "JS files that define an async function with no await" ./web
```

Use `--show-code` to confirm which runtime was chosen (the header names it). See
[Runtimes](runtimes.md) for how selection and per-runtime whitelists work.

---

## 9. macOS metadata

On macOS, `--macos-meta` reads Finder tags and download provenance (the quarantine flag
and where-from URLs) on the host and exposes them to a Python filter, so you can combine
metadata with file contents:

```bash
nfind "files tagged Red in Finder" ~/Documents --macos-meta
nfind "files downloaded from the internet that are shell scripts" ~/Downloads --macos-meta
nfind "PDFs downloaded from arxiv.org" ~/Papers --macos-meta
```

It's a no-op on other platforms. Field schema and examples:
[macOS metadata](macos-metadata.md).

---

## 10. Save a filter and replay it

`--save` writes the generated filter as a self-describing, dependency-declaring
artifact you can read, version, and re-run. Python filters are saved as
[PEP 723](https://peps.python.org/pep-0723/) scripts; Node filters are saved with a
comment header and machine-readable dependency metadata.

```bash
nfind "audio files with no album-art embedded" ~/Music --save no-art.py
```

Replay it later through the same hardened sandbox with **no LLM call** (declared packages
are still gated by the whitelist):

```bash
nfind --run no-art.py ~/Music          # with --run, the lone positional is the PATH
```

When the saved file is a Python PEP 723 script, you can also run it directly with `uv`
once you trust it (this executes on the host, outside the sandbox):

```bash
uv run no-art.py ~/Music
```

`--save` pairs naturally with `--show-code`/`--confirm` to review before keeping. See
[Saving & replaying filters](cli.md#saving--replaying-filters).

---

## 11. Tune the sandbox

Every run is bounded. Loosen or tighten the limits when a filter needs more room or you
want a tighter leash:

```bash
# Give a heavy parse more time, memory, and CPU
nfind "source files with cyclomatic complexity over 20" ./src \
  --timeout 60 --memory 1g --cpus 2

# Clamp a filter that might spawn helpers
nfind "directories that look like git repos" ~/code --pids-limit 32
```

- `--timeout` — seconds the filter may run before it's killed (default `10`).
- `--memory` — container memory limit, e.g. `256m`, `1g` (default `256m`).
- `--cpus` — CPU limit (default `1.0`).
- `--pids-limit` — max processes inside the container (default `64`).

These caps, plus the read-only mount, disabled network, and dropped capabilities, are the
[safety model](safety.md).

---

## 12. Rebuild and pin images

The base (and derived) images are cached. Force a fresh build, allow longer for a slow
build, or point at your own pre-built image:

```bash
nfind "files with no extension" --rebuild                 # rebuild the worker image first
nfind "files with no extension" --build-timeout 300       # allow 5 min for the build
nfind "files with no extension" --image my-registry/nfind-python:latest
```

`--image` overrides the base tag for the chosen runtime — handy for air-gapped or
custom-hardened images.

---

## 13. Set defaults with a config file

Tired of repeating `--model` and `--memory`? Put defaults in a TOML config file. nfind
reads `--config PATH`, then `$NFIND_CONFIG`, then
`$XDG_CONFIG_HOME/nfind/config.toml` (used only if it exists):

```toml
# ~/.config/nfind/config.toml
model = "anthropic/claude-3-5-sonnet-latest"
timeout = 30
memory = "512m"
cpus = 2
fields = true
```

```bash
nfind "Rust files that use unsafe"                  # picks up the config defaults
nfind "Rust files that use unsafe" --timeout 5      # CLI always wins over the file
nfind "Rust files that use unsafe" --config ./ci.toml
NFIND_CONFIG=./ci.toml nfind "Rust files that use unsafe"
```

Precedence is **command-line > config file > built-in default**. Settable keys mirror the
flag names (`model`, `image`, `timeout`, `memory`, `cpus`, `pids-limit`, `build-timeout`,
`json`, `fields`, `no-format`); actions like `--save`/`--run` and approval shortcuts like
`--yes`/`--no-deps` are intentionally not configurable. Full reference:
[Configuration](configuration.md#config-file).

---

## 14. Narrow and speed up big searches

On large or noisy trees, narrow what's enumerated *before* it reaches the filter. These
options run on the host, so they're deterministic and make the search faster (a smaller
path list means less work in the sandbox):

```bash
# Skip your own glob patterns (repeatable); matching directories are pruned wholesale
nfind "stale build outputs" ./project --exclude '*.min.js' --exclude dist

# Only look a couple of levels down
nfind "top-level packages" ./src --max-depth 2

# By default .git, node_modules, __pycache__, .venv, and tool caches are skipped;
# --no-ignore searches them too
nfind "anything referencing the old API" . --no-ignore
```

- `--exclude GLOB` matches each entry's name *and* its path relative to `PATH`, so
  `--exclude build` prunes every `build/` dir while `--exclude 'src/generated/*'` targets
  one spot.
- `--max-depth N` counts levels below `PATH` (a direct child is `1`).
- The default ignore set is `.git`, `.hg`, `.svn`, `node_modules`, `.venv`, `venv`,
  `__pycache__`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.tox`, `.DS_Store`.

All three also apply to `--run` replays and can be [set in the config file](#13-set-defaults-with-a-config-file).

---

## 15. Compose with other tools

Because the default output is a clean path list, nfind drops straight into Unix pipelines:

```bash
# Count matches
nfind "files larger than 100 MB" ~ | wc -l

# Act on the results safely — --print0 + xargs -0 survives spaces and newlines in names
nfind "empty directories" ~/Downloads --print0 | xargs -0 rmdir

# Feed extra fields to jq
nfind "the 20 largest files and their sizes" --json | jq '.results[] | .path'

# nfind also *reads* a path list from stdin via '-': prefilter cheaply, then analyze
find ~/data -name '*.pdf' -print0 | nfind "PDFs with embedded JavaScript, using pypdf" -

# Disable color when capturing logs (or when piping; nfind auto-detects non-TTYs)
NO_COLOR=1 nfind "TODO comments in Python files" --show-code 2> generated.py
```

nfind sits on both ends of a pipe: it emits a clean path list, and with `-` it consumes
one — newline- or NUL-delimited (auto-detected, so `find -print0` and `nfind --print0`
both feed it safely). That lets a fast mechanical filter (`find`, `fd`, `git ls-files`)
narrow the tree before nfind does the expensive content analysis.

`--print0` is the right choice whenever the results feed a destructive command: a path
with a space or newline would otherwise split into the wrong arguments. For scripting
from Python instead of the shell, see the [Python API](api.md).

---

## Option cheat sheet

| Option | Purpose |
| --- | --- |
| `PROMPT` | Natural-language description of the paths to find. |
| `PATH` | Directory to search (default: current directory). |
| `--config PATH` | TOML file of option defaults (env: `NFIND_CONFIG`). |
| `--exclude GLOB` | Skip names/paths during enumeration (repeatable; prunes dirs). |
| `--no-ignore` | Don't skip the default ignored dirs (`.git`, `node_modules`, …). |
| `--max-depth N` | Descend at most `N` levels below `PATH` (direct child = `1`). |
| `--model NAME` | Model, bare or `provider/model` (default: `openai/gpt-5.4`). |
| `--list-models` | List the selected provider's model ids and exit. |
| `--image TAG` | Override the base image for the chosen runtime. |
| `--timeout SECS` | Max filter runtime before it's killed (default: `180`). |
| `--memory SIZE` | Container memory limit (default: `256m`). |
| `--cpus N` | CPU limit (default: `1.0`). |
| `--pids-limit N` | Max processes in the container (default: `64`). |
| `--rebuild` | Rebuild the worker image before searching. |
| `--build-timeout SECS` | Seconds allowed for building the image (default: `120`). |
| `--show-code` | Print the generated filter before running it. |
| `--save PATH` | Save the filter as a replayable script with dependency metadata. |
| `--run PATH` | Replay a saved filter (no LLM call); lone positional is the PATH. |
| `--confirm`, `-i` | Show the code and ask before running. |
| `--json` | Output a `{count, results}` JSON object. |
| `--print0`, `-0` | Separate results with NUL bytes (for `xargs -0`). |
| `--fields`, `-f` | Show extra per-path fields. |
| `--yes`, `-y` | Approve requested packages without prompting. |
| `--no-deps` | Reject all third-party packages (standard library only). |
| `--no-format` | Skip the ruff cleanup of the generated filter. |
| `--macos-meta` | Expose Finder tags / download provenance (macOS only). |

Run `nfind -h` for the authoritative list, or see the full [CLI reference](cli.md).

## Where to next

- [Examples](examples.md) — a gallery of prompts that work well.
- [CLI reference](cli.md) — every option in detail.
- [Configuration](configuration.md) — env vars and the config file.
- [Safety model](safety.md) — exactly what the sandbox does and doesn't allow.
- [Python API](api.md) — drive nfind from your own code.
