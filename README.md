# nfind

[![CI](https://github.com/deeplook/nfind/actions/workflows/ci.yml/badge.svg)](https://github.com/deeplook/nfind/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/nfind.svg)](https://pypi.org/project/nfind/)
[![Python](https://img.shields.io/pypi/pyversions/nfind.svg)](https://pypi.org/project/nfind/)
[![Downloads](https://img.shields.io/pypi/dm/nfind.svg)](https://pepy.tech/project/nfind)
[![License](https://img.shields.io/pypi/l/nfind.svg)](https://pypi.org/project/nfind/)
[![Docs](https://img.shields.io/badge/docs-deeplook.github.io%2Fnfind-blue)](https://deeplook.github.io/nfind)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?style=flat&logo=buy-me-a-coffee&logoColor=black)](https://www.buymeacoffee.com/deeplook)

**Find files by describing them in natural language.**

The name is short for **n**atural-**find** — `find`, but driven by a natural-language
description instead of a filter expression. `nfind` takes a plain text description,
asks an LLM to write a small filter function for it — in Python (`filter_paths`) or
Node.js (`filterPaths`) — and runs that function against your file tree to print the
matching paths — a natural-language cousin of `find` that can answer **deep structural
questions** about your files.

The generated code is never executed on your machine directly. By default it runs
inside a **disposable, hardened Docker container** with the search directory
bind-mounted **read-only**, networking disabled, all Linux capabilities dropped, and
CPU, memory, and process limits applied. On macOS, `--sandbox apple` can opt into
Apple Containers instead; this is experimental on macOS 15 because Apple does not
support Docker-style no-network isolation there. On macOS 26+, nfind is prepared to
use Apple Containers' `--network none` support. In both cases nfind prints an explicit
warning because Apple Containers still differs from Docker's hardening surface.
`--sandbox podman` is an experimental drop-in that applies the same hardening flags as
Docker; it prints a warning until it has been validated against a real Podman runtime.

## Why nfind?

nfind sits in a gap not filled by other file-search tools. It combines three things at once:

1. **Natural language** — you describe what you want, not a query grammar or a `find`
   incantation.
2. **A standalone, auditable filter program, not a one-liner** — the LLM writes an actual
   Python/Node filter program that you can review, save, and run directly. Because it is
   a real, complete program, it can express deep structural, relational, and *computed*
   questions (e.g. "directories that contain *only* audio files", "Python files and
   their line counts") that a glob or a single `find` predicate can't.
3. **Local, disposable, hardened sandbox execution** — the program runs over your real
   tree inside a **disposable, hardened sandbox** (with your directory mounted read-only),
   so it can open and inspect files — yet your file list and contents never leave the
   machine (only your prompt is sent to the model). The default Docker backend disables
   networking. Apple Containers do it on macOS 26+.

Each neighbouring category has only part of this:

| Tool category | Natural language | Reads contents / structure | Runs locally |
| --- | :---: | :---: | :---: |
| `find` / `fd` / `ripgrep`, Spotlight (`mdfind`) | ✗ | partial | ✓ |
| `fselect` / osquery (SQL over files) | ✗ | ✓ | ✓ |
| NL→command helpers (`sgpt`, `gh copilot`) | ✓ | ✗ (just a one-liner) | ✓ |
| Send-the-file-list-to-an-LLM tools (e.g. lfind) | ✓ | ✗ (filenames only) | ✗ |
| **nfind** | **✓** | **✓** | **✓** |

Additionally, while system-level search tools like Spotlight and Siri only work locally on physical machines, `nfind` is a headless CLI tool that works perfectly over remote terminals via **SSH**, running the same sandboxed searches on your remote servers.

In one line: nfind is like asking an analyst to write and run a one-off script against
a folder — safely, and without your files leaving your machine. See
[docs/comparison.md](docs/comparison.md) for the full breakdown.

## Requirements

- Python 3.11+
- [Docker](https://docs.docker.com/get-docker/) installed and running, or an experimental
  alternate backend — Apple Containers on macOS via `--sandbox apple`, or Podman via
  `--sandbox podman` (see [Safety model](#safety-model))
- An API key for your provider — `OPENAI_API_KEY` by default, or the matching key for
  another [provider](#providers)

## Install

```bash
uv tool install nfind
# or
pip install nfind
```

To install from a local checkout:

```bash
uv tool install .
# or
pip install .
```

## Usage

```bash
export OPENAI_API_KEY=sk-...

# Search the current directory
nfind "directories that contain only audio files"

# Search a specific directory
nfind "Python files that import requests" ./src

# Search specific files (a root may be a file, not just a directory)
nfind "files that define a class" ./src/app.py ./src/models.py

# Help (both forms work)
nfind -h
nfind --help
```

### Output modes

By default `nfind` prints one path per line, like `find`. When your prompt asks
for extra per-file information, the generated filter attaches it to each result and
you can surface it:

```bash
# Default: clean, pipeable list of paths
nfind "Python files that import os"

# Verbose: path plus any extra fields the prompt produced
nfind "Python files, and for each the number of lines" --fields
# /path/to/a.py	lines=42

# JSON: machine-readable records (path plus extra fields) with a count
nfind "Python files, and for each the number of lines" --json
# { "count": 2, "results": [ { "path": "...", "lines": 42 }, ... ] }

# Extract: items inside files — one match per line (path[:line]<TAB>payload)
nfind --extract "every TODO comment, with its file and line number" ./src
# /src/app.py:42	handle retry
nfind --extract "every TODO with file and line" ./src | wc -l   # counts matches
```

Under the hood nfind does essentially one thing — **select** a subset of your files, each
optionally annotated with the extra fields your prompt asked for — and every mode above
just *renders* those records. `--extract` is the one step that reaches *inside* files
(TODOs, URLs, fields) rather than listing whole ones; counting, answering, or editing is
left to the pipe you already use (`| wc -l`, `| jq`, `| xargs`).

`--json` and `--fields` are mutually exclusive (as are `--extract` and `--fields`).
The richer output appears only when the prompt asks for it; otherwise every mode just
lists paths. See [docs/output-modes.md](docs/output-modes.md).

### Runtimes

The model picks the runtime per prompt — **Python** (default) or **Node.js**, when the
JS/TS ecosystem fits better (e.g. parsing TypeScript with `ts-morph`). nfind runs the
filter in the matching sandbox image; both run under the same isolation. See
[docs/runtimes.md](docs/runtimes.md).

```bash
nfind "TypeScript files that export a default, using ts-morph" ./src
```

### Dependencies

Some prompts need a library (reading MP3 tags, image sizes, PDF text). The generated
filter declares the packages it imports — pip for Python, npm for Node — and nfind
installs them into a derived sandbox image, but only approved packages. A built-in
default list (per runtime) installs without asking; new packages are confirmed and
then remembered.

```bash
nfind "MP3 files whose title tag contains 'live', using mutagen" ~/Music   # prompts if new
nfind "images larger than 4000px on a side" ~/Photos --yes                 # approve without asking
nfind "files containing TODO" . --no-deps                                  # standard library only
```

The Python defaults include `tree-sitter` and per-language grammar wheels
(`tree-sitter-python`, `-go`, `-rust`, …), so a filter can parse source *structure* —
functions, imports, classes — without a dedicated runtime (the Node.js runtime is
reserved for type-aware TS/JS analysis). Packages are installed at image-build time
(which needs network); the default Docker container that runs the filter has no
network. See [docs/dependencies.md](docs/dependencies.md).

### macOS metadata

On macOS, `--macos-meta` exposes a small slice of macOS-specific metadata — **Finder
tags** and **download provenance** (the quarantine flag and "where from" URLs) — to the
filter. These live on the host and aren't visible inside the Linux sandbox, so nfind
reads them host-side (read-only) and passes them in. This unlocks queries that
*combine* macOS metadata with file contents — something neither Spotlight nor a
container-only filter can do alone:

```bash
nfind "PDFs I downloaded from the web that mention 'invoice', using pypdf" ~/Downloads --macos-meta
nfind "files tagged Red whose contents contain a TODO" ~/Projects --macos-meta
```

For pure-metadata lookups ("everything tagged Red"), Spotlight (`mdfind`) is faster.
The flag is a no-op off macOS. See [docs/macos-metadata.md](docs/macos-metadata.md).

### Reviewing the generated code

The filter is generated by an LLM, so you may want to see it before it runs:

```bash
# Print the generated filter (to stderr) before running it
nfind "files with no extension" --show-code

# Save the generated filter to a file
nfind "files with no extension" --save filter.py

# Replay a saved filter through the sandbox (no LLM call; Docker also has no network)
nfind --run filter.py
nfind --run filter.py ./other-directory   # different search root

# Show the code and ask for confirmation before running (aborts on "no")
nfind "files with no extension" -i        # or --confirm
```

The code is printed to **stderr**, so stdout stays a clean, pipeable list of paths
even with `--show-code`. On a terminal it is syntax-highlighted with Pygments; the
highlighting is disabled when `NO_COLOR` is set or when stderr is redirected.

If the model's reply doesn't validate (malformed JSON, wrong function shape, an invalid
package name), nfind feeds the error back and retries a few times before giving up;
retry notices are printed to stderr.

The first run builds the worker image for the chosen runtime
(`nfind-search-paths:latest` for Python, `nfind-search-node:latest` for Node.js);
later runs reuse it. Pass `--rebuild` to force a fresh build.

### Useful options

| Option | Default | Purpose |
| --- | --- | --- |
| `--model` | `openai/gpt-5.4` | Model used to generate the filter; `provider/model` for non-OpenAI (see [Providers](#providers)) |
| `--timeout` | `180.0` | Seconds the filter may run before it is killed |
| `--memory` | `256m` | Worker container memory limit |
| `--cpus` | `1.0` | Worker container CPU limit |
| `--pids-limit` | `64` | Max processes inside the worker |
| `--sandbox` | `docker` | Sandbox backend: `docker`, experimental `apple` on macOS, or experimental `podman` |
| `--rebuild` | off | Rebuild the worker image first |
| `--exclude GLOB` | — | Skip matching names/paths during enumeration (repeatable) |
| `--no-ignore` | off | Include default ignored directories such as `.git` and `node_modules` |
| `--max-depth N` | unlimited | Descend at most `N` levels below each search path |
| `--fields` / `-f` | off | Show extra per-path fields alongside each path |
| `--json` | off | Output records (path + extra fields) as JSON |
| `--extract` | off | Explode a list-valued field into one match per line (items inside files) |
| `--extract-field NAME` | — | With `--extract`, choose the list field to explode when several exist |
| `--print0` / `-0` | off | Separate result paths with NUL bytes for `xargs -0` |
| `--yes` / `-y` | off | Approve any requested packages without prompting |
| `--no-deps` | off | Reject third-party packages (standard library only) |
| `--macos-meta` | off | macOS: expose Finder tags and download metadata to the filter |
| `--show-code` | off | Print the generated filter before running |
| `--save` | — | Write the generated filter as a replayable script |
| `--run` | — | Replay a saved filter through the sandbox without an LLM call |
| `--confirm` / `-i` | off | Show the code and confirm before running |

### Providers

By default nfind uses OpenAI. To use another provider, pass `--model provider/model`;
nfind reuses the OpenAI SDK against that provider's OpenAI-compatible endpoint, so there
is no extra dependency to install — just set the provider's API key.

```bash
nfind "files with no extension"                          # OpenAI (OPENAI_API_KEY)
nfind "..." --model anthropic/claude-sonnet-4-6          # ANTHROPIC_API_KEY
nfind "..." --model gemini/gemini-2.5-flash              # GEMINI_API_KEY
nfind "..." --model groq/llama-3.3-70b-versatile         # GROQ_API_KEY
nfind "..." --model openrouter/<vendor>/<model>          # OPENROUTER_API_KEY (near-universal)
nfind "..." --model ollama/llama3.1                      # local, no key
```

Supported prefixes: `openai`, `anthropic`, `gemini`, `groq`, `mistral`, `deepseek`,
`xai`, `openrouter`, `ollama`, `lmstudio`. Each reads its own `*_API_KEY` (local
servers need none). nfind handles providers without strict JSON mode automatically.
Capable models follow the filter contract best; weaker ones may need a retry or a
stronger model.

## Example prompts

- "directories that contain only audio files"
- "files that have no extension"
- "directories that contain more than 50 files"
- "Python virtual environments (directories with a pyvenv.cfg directly inside)"
- "initialized Terraform root modules"

## Library use

```python
from nfind import search

# Returns a list of records, each a dict with at least a "path" key (a host path).
# When the prompt asks for extra per-file values, they appear as additional keys.
records = search(".", "directories that contain only audio files")
paths = [record["path"] for record in records]
```

## Safety model

To minimize the **blast radius** of running LLM-generated code locally, `nfind` uses a sandboxed execution model that provides **strong isolation guarantees when running on Docker**:

- Search roots are mounted **read-only** under `/data`; results are mapped back to host
  paths afterward.
- The default Docker backend runs with `--network none`, `--cap-drop ALL`,
  `--security-opt no-new-privileges`, a read-only root filesystem, and a small
  `tmpfs` for scratch space.
- `--sandbox apple` uses Apple Containers. It keeps read-only mounts/root, dropped
  capabilities, CPU/memory limits, and a tmpfs. On macOS 26+ nfind uses
  `--network none`; on macOS 15 Apple does **not** support that flag, so nfind falls
  back to `--no-dns` and raw IP network access may still be possible. Apple `--cpus`
  values must be whole numbers, so fractional CPU limits are rejected.
- `--sandbox podman` uses Podman with the **same** hardened run command as Docker
  (`--network none`, dropped capabilities, `no-new-privileges`, read-only root, and
  pids/memory/CPU/tmpfs limits). It is experimental only because it has not been
  validated against a real Podman runtime yet, so nfind prints a warning before running.
- The host validates that the filter returns only paths it was given, so generated
  code cannot inject arbitrary paths into the output.
