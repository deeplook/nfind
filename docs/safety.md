# Safety model

← [Home](index.md)

pfind runs code written by a language model. That code is never executed directly on
your machine — it runs inside a disposable Docker container locked down on several
axes. This page explains exactly what the sandbox does and does not allow.

## The threat

An LLM-generated filter could, in principle, be wrong, ambiguous, or malicious: it
might try to delete or modify files, exfiltrate data over the network, or consume
unbounded resources. pfind's design assumes the generated code is **untrusted** and
contains it accordingly.

## What protects you

### 1. Code generation is separated from execution

The prompt is turned into code **on the host**, where your OpenAI credentials live.
Only your natural-language description is sent to the API — never your file list or
file contents. The generated code is then shipped into the sandbox to run. The
sandbox itself has no network and no credentials.

### 2. The search tree is mounted read-only

The directory you search is bind-mounted at `/data` with the `readonly` flag. The
filter can read names, metadata, and contents, but **cannot create, modify, or delete
anything** in your files.

### 3. The container is hardened

This applies to both [runtimes](runtimes.md): the Python and Node.js base images run
under the same restrictions. Each run uses a fresh, throwaway container started with:

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

### 4. Results can't be forged

The host gives the filter a fixed list of paths and **verifies that every returned
result is one of them**. Generated code therefore cannot inject arbitrary paths into
the output, and the host maps only known container paths back to host paths.

### 5. Third-party packages are gated by a whitelist

A filter may request PyPI packages (to read MP3 tags, image metadata, and so on).
pfind installs only packages that are **approved** — a curated built-in list plus
ones you've explicitly approved before, remembered across runs. New packages require
confirmation; [`--no-deps`](cli.md#options) refuses them entirely. Packages are
installed at image-build time (which needs network); the container that runs the
filter still has no network. See [Dependencies & the whitelist](dependencies.md).

### 6. You can review before running

For an extra layer of human control, inspect the code before it executes:

- [`--show-code`](cli.md#reviewing-the-generated-code) prints the generated filter.
- [`--save`](cli.md#reviewing-the-generated-code) writes it to a file for review.
- [`--confirm` / `-i`](cli.md#reviewing-the-generated-code) shows it and asks for
  approval; declining aborts before anything runs.

## What this does *not* protect against

- **Container-escape vulnerabilities** in Docker itself. pfind relies on Docker's
  isolation; keep Docker updated. For higher assurance, run pfind on a machine where
  the worst case is acceptable.
- **Risk inside an approved package.** Approving a package trusts its install-time and
  import-time behaviour. Only approve packages you recognise; the whitelist limits
  *which* packages can enter the image, not what a given package does.
- **Information disclosure within the mounted tree.** The filter can read everything
  under the path you search. Only point pfind at directories whose contents you're
  comfortable having an LLM-written script read. Results (paths, and any extra fields)
  return to your terminal, not to the network.
- **Cost or rate limits** of the OpenAI API — that's between the host and the API.

## Summary

The generated code is treated as untrusted: it runs with no network, no write access
to your files, dropped privileges, and bounded resources, in a container that is
discarded immediately. The strongest practical guarantee is the read-only mount plus
no network — the filter can look, but it cannot touch your files or phone home.
