# Installation

← [Home](index.md)

Install via `uv`, `pipx`, or `pip` — `uv tool install` is the recommended method:

```bash
# Try without installing (always fetches the latest release)
uvx nfind@latest --help

# Recommended: isolated tool install via uv (fastest)
uv tool install nfind

# Alternative: pipx (install pipx first if needed: brew install pipx on macOS)
pipx install nfind

# Into the current environment
pip install nfind
```

## How to run nfind

| Method / Install | Command | Best for |
|---|---|---|
| `uv tool install nfind` | `nfind "…"` | Day-to-day use — isolated install, fast startup |
| `uvx nfind` (no install) | `uvx nfind "…"` | Trying it out or one-off use; always runs the latest release |
| `pip install nfind` | `from nfind import search` | Scripting, automation, notebooks — see [Python API](api.md) |

## Prerequisites

nfind needs two things at runtime in addition to Python:

| Requirement | Used for | macOS | Debian/Ubuntu | Check |
|---|---|---|---|---|
| [Docker](https://docs.docker.com/get-docker/) | Default sandboxed execution of the generated filter | [Docker Desktop](https://docs.docker.com/desktop/mac/install/) or `brew install docker` | `sudo apt install docker.io` | `docker info` |
| [Apple Containers](https://github.com/apple/container) | Experimental alternate sandbox via `--sandbox apple` | Install Apple `container`, then `container system start` | — | `container system status` |
| OpenAI API key | Generating the filter from your prompt | — | — | `echo $OPENAI_API_KEY` |

Set your API key before running:

```bash
export OPENAI_API_KEY=sk-...
```

The host needs network access to reach the OpenAI API, but code generation happens on
the host, where your credentials stay. With the default Docker backend, the worker
container has networking disabled. With `--sandbox apple` on macOS 15, Apple
Containers does not support Docker-style `--network none`; nfind prints a warning and
uses the weaker `--no-dns` flag instead. See the [Safety model](safety.md) for the full
picture.

## The worker image

On first use, nfind builds a small base worker image for the chosen
[runtime](runtimes.md) and reuses it on later runs:

- **Python** — `nfind-search-paths:latest` (based on `python:3.11-slim`)
- **Node.js** — `nfind-search-node:latest` (based on `node:22-slim`)

Each base needs only its standard runtime. Force a rebuild with
[`--rebuild`](cli.md#options), or override the base tag with
[`--image`](cli.md#options).

When a prompt needs a third-party library, nfind builds a **derived** image
(`…:deps-<hash>`) that layers the approved packages (pip or npm) on top of the base,
and caches it for reuse. See [Dependencies & the whitelist](dependencies.md).

Building images requires the selected container backend to pull `python:3.11-slim`
once (and, for derived images, to reach PyPI). After that, Docker searches work offline
apart from the OpenAI API call; Apple Containers on macOS 15 may still have raw IP
network access at run time because Apple does not support `--network none` there.
