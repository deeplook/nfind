# Installation

← [Home](index.md)

Install via `uv`, `pipx`, or `pip` — `uv tool install` is the recommended method:

```bash
# Try without installing (always fetches the latest release)
uvx pfind@latest --help

# Recommended: isolated tool install via uv (fastest)
uv tool install pfind

# Alternative: pipx (install pipx first if needed: brew install pipx on macOS)
pipx install pfind

# Into the current environment
pip install pfind
```

## How to run pfind

| Method / Install | Command | Best for |
|---|---|---|
| `uv tool install pfind` | `pfind "…"` | Day-to-day use — isolated install, fast startup |
| `uvx pfind` (no install) | `uvx pfind "…"` | Trying it out or one-off use; always runs the latest release |
| `pip install pfind` | `from pfind import search` | Scripting, automation, notebooks — see [Python API](api.md) |

## Prerequisites

pfind needs two things at runtime in addition to Python:

| Requirement | Used for | macOS | Debian/Ubuntu | Check |
|---|---|---|---|---|
| [Docker](https://docs.docker.com/get-docker/) | Sandboxed execution of the generated filter | [Docker Desktop](https://docs.docker.com/desktop/mac/install/) or `brew install docker` | `sudo apt install docker.io` | `docker info` |
| OpenAI API key | Generating the filter from your prompt | — | — | `echo $OPENAI_API_KEY` |

Set your API key before running:

```bash
export OPENAI_API_KEY=sk-...
```

The host needs network access to reach the OpenAI API, but the **worker container
never does** — code generation happens on the host, where your credentials stay; the
sandbox that runs the generated code has networking disabled. See the
[Safety model](safety.md) for the full picture.

## The worker image

On first use, pfind builds a small base worker image for the chosen
[runtime](runtimes.md) and reuses it on later runs:

- **Python** — `pfind-search-paths:latest` (based on `python:3.12-slim`)
- **Node.js** — `pfind-search-node:latest` (based on `node:22-slim`)

Each base needs only its standard runtime. Force a rebuild with
[`--rebuild`](cli.md#options), or override the base tag with
[`--image`](cli.md#options).

When a prompt needs a third-party library, pfind builds a **derived** image
(`…:deps-<hash>`) that layers the approved packages (pip or npm) on top of the base,
and caches it for reuse. See [Dependencies & the whitelist](dependencies.md).

Building images requires Docker to pull `python:3.12-slim` once (and, for derived
images, to reach PyPI). After that, searches work offline apart from the OpenAI API
call — the container that runs the filter never has network access.
