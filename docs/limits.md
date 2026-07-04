# Limits and scaling

nfind has several independent stages, and each has different limits. A search can be
slow before the generated filter starts, so `--timeout 180` is **not** a deadline for the
whole command.

## Current defaults

| Area | Default | What it covers |
| --- | --- | --- |
| Directory depth | unlimited | Recursive host-side enumeration below each directory root |
| Number of input paths | unlimited | Files and directories passed to the generated filter |
| Input file size | unlimited | Files the generated filter chooses to open |
| Image build time | 120 seconds | Building a base or dependency image (`--build-timeout`) |
| Filter execution time | 180 seconds | Running the generated filter in the sandbox (`--timeout`) |
| Whole-command time | unlimited | Optional wall-clock deadline (`--command-timeout`) |
| Worker memory | 256 MB | Sandbox memory available to the worker (`--memory`) |
| Worker CPU | 1 CPU | Sandbox CPU allocation (`--cpus`) |
| Worker processes | 64 | Sandbox process limit (`--pids-limit`) |
| Worker response | 1,000,000 bytes | Internal JSON response from the sandbox to nfind |
| Number of results | unlimited | Configurable with `--max-results` |
| Extracted items | unlimited | Configurable with `--max-items` |
| Rendered stdout | unlimited | Configurable with `--max-output-bytes` |

The 1 MB worker-response ceiling is a fixed protocol safety limit. Exceeding it fails the
search; nfind does not currently truncate the result. It is not the same as a configurable
user-facing output limit.

## Where time is spent

A generated search proceeds through several stages:

1. Check the selected sandbox runtime.
2. Walk the search roots and collect paths.
3. Ask the model to generate and validate a filter, retrying invalid replies.
4. Build an image when the base image or requested dependencies are not cached.
5. Run the generated filter inside the sandbox.
6. Validate, map, and render the returned records.

`--timeout` applies only to stage 5. It does not include directory enumeration, model
latency, validation retries, image builds, or host-side rendering. `--build-timeout`
applies to stage 4 separately. Use `--command-timeout` for an optional wall-clock
deadline covering the complete invocation.

`--command-timeout` currently requires POSIX interval timers (macOS/Linux). nfind rejects
the option on platforms that cannot safely interrupt and clean up a running sandbox.

## Input scale

Directory roots are walked recursively by default. Common VCS, dependency,
virtual-environment, and cache names are pruned unless `--no-ignore` is set. Use the
host-side controls to reduce work before paths reach the sandbox:

```bash
nfind "Python files importing requests" ./src \
  --exclude generated \
  --exclude '*.min.js' \
  --max-depth 6
```

There is no fixed maximum number of enumerated paths. The path list is serialized to JSON
and then deserialized inside the worker, so very large trees consume memory on both sides
of the sandbox boundary. With the default 256 MB worker limit, roughly 500,000–1,000,000
paths may exhaust memory; the actual point depends on path length and generated-filter
behavior.

Prefer a narrower root, `--exclude`, `--max-depth`, or an upstream `find`/`fd` pipeline.
Raise `--memory` only when the broader input is intentional:

```bash
find ./data -type f -name '*.json' -print0 | \
  nfind "JSON files containing duplicate keys" - --memory 1g
```

## File sizes

nfind does not impose a per-file size limit. The model writes the filter, and that filter
decides whether to stream a file, read it fully into memory, or use a parsing library.
Large media files, archives, minified files, and generated indexes can therefore dominate
execution time or memory.

Constrain the roots or prefilter by size when possible:

```bash
find ./logs -type f -size -20M -print0 | \
  nfind "logs containing an unhandled exception" -
```

Increasing `--timeout` helps only when the generated filter is making useful progress.
Increasing `--memory` may be required when its parser loads whole files.

## Results and output

The generated filter returns path records through an internal JSON protocol. The encoded
worker response must not exceed 1,000,000 bytes. This fixed ceiling is enforced before
user-facing limits, so `--max-results` cannot rescue a worker response that is already too
large.

Use `--max-results` to retain only complete path records, `--max-items` to bound text rows
emitted by `--extract`, and `--max-output-bytes` as the final stdout boundary. Limits are
opt-in. Text modes stop before a partial row and warn on stderr; JSON remains valid and
reports `truncated` plus `truncated_by` metadata. Intentional truncation exits successfully.

Broad `--extract` queries can still produce inconvenient output, particularly when an
item contains an entire minified line. Narrow the input roots and describe the intended
file types explicitly:

```bash
nfind "TODO comments in Python source files, with line and text fields" \
  ./src ./tests --extract
```

```bash
nfind "TODO comments in Python source files" ./src --extract \
  --max-results 1000 --max-items 5000 --max-output-bytes 10000000
```

## Choosing limits

- Narrow inputs first; this reduces enumeration, serialization, and filter work.
- Increase `--timeout` for filters that legitimately inspect many or large files.
- Increase `--memory` for large path lists or parsers that load complete files.
- Increase `--cpus` only when the generated filter or its libraries can use them.
- Increase `--build-timeout` for slow image pulls or dependency installation.
- Set `--command-timeout` when automation needs a deadline for the complete invocation.
- Add result/output limits when broad or model-generated selection may be noisy.
- Avoid `--no-ignore` on large repositories unless ignored trees are the search target.

Resource options can be stored in the [configuration file](configuration.md), but project-
specific roots and exclusions usually belong in the command itself.
