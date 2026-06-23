# nfind

**Find files by describing them in natural language.**

The name is short for **n**atural-**find** — `find`, but driven by a natural-language
description instead of a filter expression. `nfind` takes a plain-English description, asks an LLM to write
a small filter function for it — in Python (`filter_paths`) or Node.js (`filterPaths`) —
and runs that function against your file tree to print the matching paths — a
natural-language cousin of `find`.

The generated code is never executed on your machine directly. It runs inside a
**disposable, hardened Docker container** with the search directory bind-mounted
**read-only**, networking disabled, all Linux capabilities dropped, and CPU, memory,
and process limits applied. The container can therefore inspect file contents and
metadata to answer richer questions, but it cannot modify your files or reach the
network.

## Why nfind?

nfind sits in a gap no other file-search tool fills. It combines three things at once:

1. **Natural language** — you describe what you want, not a query grammar or a `find`
   incantation.
2. **A real generated program, not a one-liner** — the LLM writes an actual
   Python/Node filter, so it can express structural, relational, and *computed*
   questions (e.g. "directories that contain *only* audio files", "Python files and
   their line counts") that a glob or a single `find` predicate can't.
3. **Local, sandboxed execution that reads file contents** — the program runs over
   your real tree in a read-only, no-network container, so it can open and inspect
   files — yet your file list and contents never leave the machine (only your prompt
   is sent to the model).

Each neighbouring category has only part of this:

| Tool category | Natural language | Reads contents / structure | Runs locally |
| --- | :---: | :---: | :---: |
| `find` / `fd` / `ripgrep`, Spotlight (`mdfind`) | ✗ | partial | ✓ |
| `fselect` / osquery (SQL over files) | ✗ | ✓ | ✓ |
| NL→command helpers (`sgpt`, `gh copilot`) | ✓ | ✗ (just a one-liner) | ✓ |
| Send-the-file-list-to-an-LLM tools (e.g. lfind) | ✓ | ✗ (filenames only) | ✗ |
| **nfind** | **✓** | **✓** | **✓** |

In one line: nfind is like asking an analyst to write and run a one-off script against
a folder — safely, and without your files leaving your machine. See
[docs/comparison.md](docs/comparison.md) for the full breakdown.

## Requirements

- Python 3.11+
- [Docker](https://docs.docker.com/get-docker/) installed and running
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
nfind "Python files, and for each the number of lines" --verbose
# /path/to/a.py	lines=42

# JSON: machine-readable records (path plus extra fields) with a count
nfind "Python files, and for each the number of lines" --json
# { "count": 2, "results": [ { "path": "...", "lines": 42 }, ... ] }
```

`--json` and `--verbose` are mutually exclusive. The richer output appears only
when the prompt asks for it; otherwise every mode just lists paths.

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
(which needs network); the container that runs the filter still has no network. See
[docs/dependencies.md](docs/dependencies.md).

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

# Show the code and ask for confirmation before running (aborts on "no")
nfind "files with no extension" -i        # or --confirm
```

The code is printed to **stderr**, so stdout stays a clean, pipeable list of paths
even with `--show-code`. On a terminal it is syntax-highlighted with Pygments; the
highlighting is disabled when `NO_COLOR` is set or when stderr is redirected.

If the model's reply doesn't validate (malformed JSON, wrong function shape, an invalid
package name), nfind feeds the error back and retries a few times before giving up;
`--verbose` reports when a retry happens.

The first run builds the worker image for the chosen runtime
(`nfind-search-paths:latest` for Python, `nfind-search-node:latest` for Node.js);
later runs reuse it. Pass `--rebuild` to force a fresh build.

### Useful options

| Option | Default | Purpose |
| --- | --- | --- |
| `--model` | `gpt-4o-mini` | Model used to generate the filter; `provider/model` for non-OpenAI (see [Providers](#providers)) |
| `--timeout` | `10.0` | Seconds the filter may run before it is killed |
| `--memory` | `256m` | Worker container memory limit |
| `--cpus` | `1.0` | Worker container CPU limit |
| `--pids-limit` | `64` | Max processes inside the worker |
| `--rebuild` | off | Rebuild the worker image first |
| `--exclude GLOB` | — | Skip matching names/paths during enumeration (repeatable) |
| `--no-ignore` | off | Include default ignored directories such as `.git` and `node_modules` |
| `--max-depth N` | unlimited | Descend at most `N` levels below each search path |
| `--verbose` / `-v` | off | Show extra per-path fields alongside each path |
| `--json` | off | Output records (path + extra fields) as JSON |
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

- Search roots are mounted **read-only** under `/data`; results are mapped back to host
  paths afterward.
- The worker container runs with `--network none`, `--cap-drop ALL`,
  `--security-opt no-new-privileges`, a read-only root filesystem, and a small
  `tmpfs` for scratch space.
- The host validates that the filter returns only paths it was given, so generated
  code cannot inject arbitrary paths into the output.
