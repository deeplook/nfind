# nfind

**nfind** — short for **n**atural-**find** — finds files by describing them in plain
language (it's `find`, but driven by a natural-language description instead of a filter
expression). You give a natural-language prompt; an LLM writes a small Python filter for
it; that filter runs against your file tree inside a hardened, disposable Docker
container and prints the matching paths — a natural-language cousin of `find`.

```bash
uv tool install nfind
export OPENAI_API_KEY=sk-...
nfind "directories that contain only audio files"
nfind "Python files that import requests" ./src
```

## What can you ask?

You can ask **deep structural questions** about your files. The prompt is free-form.
These are examples of the kind of query that isn't possible with `find` or `grep` —
they show why generating a real program per question matters:

```bash
# Cross-file structural analysis
nfind "Helm charts where replicaCount in values.yaml defaults to 1 but the Deployment template never overrides it" ~/k8s

# Semantic code quality
nfind "JavaScript files that define async functions but never handle Promise rejections" ./src

# Security: context-sensitive pattern analysis
nfind "shell scripts that pass an unquoted variable to rm -rf" ~/bin

# Binary introspection
nfind "MP3 files whose embedded cover art is larger in bytes than the audio data itself" ~/Music

# macOS provenance × file contents (Spotlight can do each alone; nfind combines them)
nfind "PDFs I downloaded from arxiv.org that mention 'mechanistic interpretability'" ~/Papers --macos-meta
```

Because nfind **generates a real program per query** — rather than matching fixed
predicates or sending your files to a model — the answer is: almost anything you can
describe in a sentence. See the [Examples gallery](examples.md).

## How it works

1. **Enumerate** — nfind walks the search directory on the host and collects every
   file and directory path.
2. **Generate** — it asks an LLM to write a filter function matching your prompt (the
   path list itself is *not* sent — only your description). The model also picks the
   [runtime](runtimes.md) — Python or Node.js — and declares any packages it needs.
   If the reply doesn't validate, nfind feeds the error back and retries a few times.
   The generated Python filter is then tidied with ruff (unused imports removed, imports
   sorted, reformatted) before it is shown, saved, or run.
3. **Run safely inside a disposable, hardened sandbox** — by default, the generated
   code executes inside a **disposable, hardened sandbox** (a throwaway Docker container)
   with the search root bind-mounted **read-only**, networking disabled, all Linux
   capabilities dropped, and CPU/memory/process limits applied to minimize the **blast
   radius**. On macOS, `--sandbox apple` opts into experimental Apple Containers support
   with an explicit macOS 15 networking warning; `--sandbox podman` is an experimental
   drop-in that applies the same hardening flags as Docker.
4. **Map back** — the container returns matching container paths; nfind maps them to
   host paths and prints them.

Because the filter runs inside the sandbox, it can safely inspect file *contents* and
metadata — not just names — to answer questions classic `find` + `grep` typically can't.

<!-- diagram: architecture — to be added -->

## Why it exists

Tools like [lfind](https://pypi.org/project/lfind/) send the whole file list to an
LLM and let the model do the filtering — which doesn't scale and only sees filenames.
nfind instead has the LLM write code once, then runs that code locally over the full
tree. It scales to large directories, can read file contents, and keeps you in
control: you can review, save, or confirm the generated code before it runs.

Unlike Spotlight (`mdfind`), which queries a pre-built metadata index, nfind generates
and runs a program per question — so it can answer structural and computed queries an
attribute index can't express. See [How nfind compares](comparison.md).

## Features

- **Natural-language search** over any directory tree.
- **[Sandboxed execution](safety.md)** — read-only mount, dropped caps, resource
  limits; the default Docker backend also disables networking.
- **[Review before running](cli.md#reviewing-the-generated-code)** —
  [`--show-code`](cli.md#options), [`--save`](cli.md#options), and
  [`--confirm`](cli.md#options) let you inspect, keep, or approve the generated filter.
- **[Save & replay](cli.md#saving--replaying-filters)** — `--save` writes the filter as
  a **standalone, auditable filter program** (a self-describing, dependency-declaring artifact), guaranteeing perfect **reproducibility**; replay it sandboxed with `--run`
  or run trusted Python saves directly via `uv run`.
- **[Output modes](output-modes.md)** — a clean path list by default, `--fields` for
  extra per-path fields, `--json` for machine-readable records.
- **[Declared dependencies](dependencies.md)** — filters can request libraries (to read
  MP3 tags, image sizes, …); approved packages are installed into a derived sandbox
  image and remembered, gated by a whitelist.
- **[Python & Node.js runtimes](runtimes.md)** — the model picks the ecosystem per
  prompt (e.g. TypeScript analysis with `ts-morph`); nfind runs the filter in the
  matching sandbox image.
- **[macOS metadata](macos-metadata.md)** — `--macos-meta` exposes Finder tags and
  download provenance to the filter, enabling queries that combine them with file
  contents.
- **[Python API](api.md)** — call `search()` from your own code.

## Quick start

```bash
nfind "files with no extension"                        # search the current directory
nfind "directories with more than 50 files" ~/Projects # search a specific directory
nfind "Python files, and for each the number of lines" --fields
nfind "audio files (mp3, flac, wav)" --json
nfind "files that look like backups" --confirm         # review the code first
```

See [Examples](examples.md) for the full prompt gallery.

## Requirements

- Python 3.11+
- [Docker](https://docs.docker.com/get-docker/) installed and running, or an experimental
alternate backend — [Apple Containers](https://opensource.apple.com/projects/container/)
on macOS, or [Podman](https://podman.io/) - An OpenAI API key in `OPENAI_API_KEY` (or
another [provider's](cli.md#providers) key)

See [Installation](installation.md) for details.

## Documentation

### Get started

- [Installation](installation.md) — install via `uv`, `pipx`, or `pip`; Docker and API key setup
- [Tutorial](getting-started.md) — a hands-on walkthrough of every feature, from first search to advanced options
- [Examples](examples.md) — a gallery of prompts to adapt, with runtime and image information

### Reference

- [CLI reference](cli.md)
- [Configuration](configuration.md) — env vars, config file, and model/provider selection
- [Output modes](output-modes.md)
- [Python API](api.md)

### Concepts

- [Safety model](safety.md) — what the sandbox does and doesn't protect
- [Runtimes (Python & Node.js)](runtimes.md) — how the model picks a runtime and why only two exist
- [Dependencies & the whitelist](dependencies.md) — third-party packages inside the sandbox
- [macOS metadata](macos-metadata.md) — Finder tags and download provenance
- [How nfind compares](comparison.md) — nfind vs. Spotlight, `find`, lfind, and others

### Help

- [Troubleshooting](troubleshooting.md)

## Transparency

A significant portion of this codebase was developed with AI assistance (primarily
Claude by Anthropic). All generated code was reviewed and curated by the author.
