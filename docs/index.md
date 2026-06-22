# pfind

**pfind** — short for **p**rompt(ed)-**find** — finds files by describing them in plain
language (it's `find`, but driven by a prompt instead of a filter expression). You give
a natural-language prompt; an LLM writes a small Python filter for it; that filter
runs against your file tree inside a hardened, disposable Docker container and prints
the matching paths — a natural-language cousin of `find`.

```bash
uv tool install pfind
export OPENAI_API_KEY=sk-...
pfind "directories that contain only audio files"
pfind "Python files that import requests" ./src
```

## How it works

1. **Enumerate** — pfind walks the search directory on the host and collects every
   file and directory path.
2. **Generate** — it asks an LLM to write a filter function matching your prompt (the
   path list itself is *not* sent — only your description). The model also picks the
   [runtime](runtimes.md) — Python or Node.js — and declares any packages it needs.
   If the reply doesn't validate, pfind feeds the error back and retries a few times.
   The generated Python filter is then tidied with ruff (unused imports removed, imports
   sorted, reformatted) before it is shown, saved, or run.
3. **Run safely** — the generated code executes in a throwaway Docker container with
   the search root bind-mounted **read-only**, networking disabled, all Linux
   capabilities dropped, and CPU/memory/process limits applied.
4. **Map back** — the container returns matching container paths; pfind maps them to
   host paths and prints them.

Because the filter runs inside the sandbox, it can safely inspect file *contents* and
metadata — not just names — to answer questions classic `find` + `grep` can't.

## Why it exists

Tools like `lfind` send the whole file list to an LLM and let the model do the
filtering — which doesn't scale and only sees filenames. pfind instead has the LLM
write code once, then runs that code locally over the full tree. It scales to large
directories, can read file contents, and keeps you in control: you can review, save,
or confirm the generated code before it runs.

Unlike Spotlight (`mdfind`), which queries a pre-built metadata index, pfind generates
and runs a program per question — so it can answer structural and computed queries an
attribute index can't express. See [How pfind compares](comparison.md).

## Features

- **Natural-language search** over any directory tree.
- **[Sandboxed execution](safety.md)** — read-only mount, no network, dropped caps,
  resource limits; the generated code cannot modify your files or reach the network.
- **[Review before running](cli.md#reviewing-the-generated-code)** —
  [`--show-code`](cli.md#options), [`--save`](cli.md#options), and
  [`--confirm`](cli.md#options) let you inspect, keep, or approve the generated filter.
- **[Save & replay](cli.md#saving--replaying-filters)** — `--save` writes the filter as
  a self-describing, dependency-declaring PEP 723 script; replay it sandboxed with
  `--run` or run it directly (if trusted) via `uv run`.
- **[Output modes](output-modes.md)** — a clean path list by default, `--verbose` for
  extra per-path fields, `--json` for machine-readable records.
- **[Declared dependencies](dependencies.md)** — filters can request libraries (to read
  MP3 tags, image sizes, …); approved packages are installed into a derived sandbox
  image and remembered, gated by a whitelist.
- **[Python & Node.js runtimes](runtimes.md)** — the model picks the ecosystem per
  prompt (e.g. TypeScript analysis with `ts-morph`); pfind runs the filter in the
  matching sandbox image.
- **[macOS metadata](macos-metadata.md)** — `--macos-meta` exposes Finder tags and
  download provenance to the filter, enabling queries that combine them with file
  contents.
- **[Python API](api.md)** — call `search()` from your own code.

## Quick start

```bash
pfind "files with no extension"                        # search the current directory
pfind "directories with more than 50 files" ~/Projects # search a specific directory
pfind "Python files, and for each the number of lines" --verbose
pfind "audio files (mp3, flac, wav)" --json
pfind "files that look like backups" --confirm         # review the code first
```

See [Examples](examples.md) for the full prompt gallery.

## Requirements

- Python 3.12+
- [Docker](https://docs.docker.com/get-docker/) installed and running
- An OpenAI API key in `OPENAI_API_KEY` (or another [provider's](cli.md#providers) key)

See [Installation](installation.md) for details.

## Documentation

- [Installation](installation.md)
- [Getting started](getting-started.md)
- [Configuration](configuration.md)
- [CLI reference](cli.md)
- [Examples](examples.md)
- [Output modes](output-modes.md)
- [Dependencies & the whitelist](dependencies.md)
- [Runtimes (Python & Node.js)](runtimes.md)
- [macOS metadata](macos-metadata.md)
- [How pfind compares](comparison.md)
- [Safety model](safety.md)
- [Python API](api.md)
- [Troubleshooting](troubleshooting.md)

## Transparency

A significant portion of this codebase was developed with AI assistance (primarily
Claude by Anthropic). All generated code was reviewed and curated by the author.
