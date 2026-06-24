# Safety model

← [Home](index.md)

nfind runs code written by a language model. That code is never executed directly on
your machine — it runs inside a disposable container locked down on several axes. The
default backend is Docker; macOS users may explicitly opt into Apple Containers with
`--sandbox apple`. This page explains exactly what each sandbox does and does not
allow.

## The threat

An LLM-generated filter could, in principle, be wrong, ambiguous, or malicious: it
might try to delete or modify files, exfiltrate data over the network, or consume
unbounded resources. nfind's design assumes the generated code is **untrusted** and
contains it accordingly.

## What protects you

### 1. Code generation is separated from execution

The prompt is turned into code **on the host**, where your OpenAI credentials live.
Only your natural-language description is sent to the API — never your file list or
file contents. The generated code is then shipped into the sandbox to run. The
sandbox itself has no credentials. With the default Docker backend it also has no
network. With Apple Containers on macOS 15, see the network caveat below.

### 2. The search tree is mounted read-only

The directory you search is bind-mounted at `/data` with the `readonly` flag. The
filter can read names, metadata, and contents, but **cannot create, modify, or delete
anything** in your files.

### 3. The default Docker container is hardened

This applies to both [runtimes](runtimes.md): the Python and Node.js base images run
under the same restrictions. Each Docker run uses a fresh, throwaway container started
with:

| Flag | Effect |
|---|---|
| `--network none` | No network access of any kind. |
| `--read-only` | The container's own root filesystem is read-only. |
| `--cap-drop ALL` | All Linux capabilities dropped. |
| `--security-opt no-new-privileges` | Processes cannot gain new privileges. |
| `--mount …,readonly` | The search tree is read-only (see above). |
| `--tmpfs /tmp:…,noexec,nosuid,nodev` | A small scratch space, non-executable. |
| `--memory`, `--cpus`, `--pids-limit`, `--ulimit nofile` | Bounded memory, CPU, processes, and open files. |
| `--rm` | The container is removed when it exits. |

The worker also runs as an unprivileged user inside the image. If the filter exceeds
its [`--timeout`](cli.md#options), the container is killed.

### 3b. Apple Containers is an explicit experimental backend

`--sandbox apple` uses Apple's `container` CLI instead of Docker. nfind uses the same
worker images and keeps the important file-system protections: the search roots are
mounted read-only, the container root filesystem is read-only, capabilities are
dropped, CPU/memory/open-file limits are set, and a tmpfs is provided for scratch
space. On macOS 26+ the backend passes `--network none`; on older macOS releases it
falls back to `--no-dns`.

However, this is **not security-equivalent to Docker on macOS 15**. Apple's official
docs say that on macOS 15 all containers attach to the default vmnet network, the
`container network` commands are unavailable, and using `--network` with
`container run` or `container create` results in an error. As a result, nfind cannot
pass Docker's `--network none` on macOS 15. `--no-dns` only avoids configuring DNS; it
does not prove that raw IP network access is impossible. On macOS 26+ nfind is prepared
to use Apple Containers' `--network none` support instead. The current Apple CLI also
does not expose Docker-equivalent `--pids-limit` or
`--security-opt no-new-privileges` flags. Its `--cpus` option accepts whole-number CPU
counts only; nfind formats the default `1.0` as `1` and rejects fractional Apple CPU
limits before running the container.

Because of this, Apple Containers is opt-in and prints a warning every time it is used.
Use Docker when you need nfind's strongest sandbox. Apple Containers is useful on macOS
15 when you accept this network limitation in exchange for running via Apple's
lightweight VM-per-container runtime, and is prepared to use stronger network isolation
on macOS 26+.

### 4. Results can't be forged

The host gives the filter a fixed list of paths and **verifies that every returned
result is one of them**. Generated code therefore cannot inject arbitrary paths into
the output, and the host maps only known container paths back to host paths.

### 5. Third-party packages are gated by a whitelist

A filter may request PyPI packages (to read MP3 tags, image metadata, and so on).
nfind installs only packages that are **approved** — a curated built-in list plus
ones you've explicitly approved before, remembered across runs. New packages require
confirmation; [`--no-deps`](cli.md#options) refuses them entirely. Packages are
installed at image-build time (which needs network). The default Docker container that
runs the filter has no network; the experimental Apple backend provides that guarantee
only on macOS 26+ where `--network none` is available, not on macOS 15. See
[Dependencies & the whitelist](dependencies.md).

### 6. You can review before running

For an extra layer of human control, inspect the code before it executes:

- [`--show-code`](cli.md#reviewing-the-generated-code) prints the generated filter.
- [`--save`](cli.md#saving--replaying-filters) writes it to a self-describing,
  replayable artifact for review (and later `--run`). Note that running a saved Python
  filter directly with `uv run` executes it **outside** this sandbox — only do so for
  filters you trust; use `nfind --run` to replay it sandboxed.
- [`--confirm` / `-i`](cli.md#reviewing-the-generated-code) shows it and asks for
  approval; declining aborts before anything runs.

## What this does *not* protect against

- **Container-runtime vulnerabilities** in Docker or Apple Containers. nfind relies on
  the selected runtime's isolation; keep it updated. For higher assurance, run nfind on
  a machine where the worst case is acceptable.
- **Risk inside an approved package.** Approving a package trusts its install-time and
  import-time behaviour. Only approve packages you recognise; the whitelist limits
  *which* packages can enter the image, not what a given package does.
- **Apple Containers networking on macOS 15.** The Apple backend does not have a
  Docker-equivalent `--network none` mode on macOS 15. It warns and uses `--no-dns`,
  but raw IP network access may still be possible.
- **Information disclosure within the mounted tree.** The filter can read everything
  under the path you search. Only point nfind at directories whose contents you're
  comfortable having an LLM-written script read. Results (paths, and any extra fields)
  return to your terminal, not to the network.
- **Cost or rate limits** of the OpenAI API — that's between the host and the API.

## Summary

The generated code is treated as untrusted. With the default Docker backend, it runs
with no network, no write access to your files, dropped privileges, and bounded
resources, in a container that is discarded immediately. With Apple Containers on
macOS 15, the read-only file protections remain, but no-network isolation is not
available; use it only when that tradeoff is acceptable.
