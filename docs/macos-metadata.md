# macOS metadata (`--macos-meta`)

← [Home](index.md)

> **Status: prototype.** Two attribute groups are wired up (Finder tags and download
> provenance). The flag is a no-op on non-macOS hosts.

Some of the most useful things you know about a file on a Mac aren't in its name or
bytes — they live in macOS-specific metadata: the **Finder tags** you applied, and the
**download provenance** (the quarantine flag and "where from" URLs) the system records
when you save a file from the web.

`--macos-meta` makes a small, high-value slice of that metadata available to the filter.

```bash
nfind "PDFs I downloaded from the web that mention 'invoice', using pypdf" ~/Downloads --macos-meta
nfind "files tagged Red whose contents contain a TODO" ~/Projects --macos-meta
```

## Why this needs special support

nfind's filter runs inside a **Linux** container, against your files bind-mounted
read-only at `/data`. macOS metadata is *not* visible there: Spotlight's index lives
only on the host, and extended attributes (tags, quarantine, where-from) do not
reliably survive Docker's file-sharing layer into the container. So a filter simply
*can't read* these attributes by itself.

`--macos-meta` closes that gap **without weakening the sandbox**: nfind reads the
attributes on the host (a read-only operation, where the data actually lives) during
the path walk, then passes them into the container alongside the paths. The untrusted
generated code still runs sandboxed — no network, read-only mount, dropped
capabilities — and never calls back to the host.

## What it buys you

The unique benefit is queries that **combine** macOS metadata with file *contents* or
computed *structure* — something neither tool below can do alone:

| Query | Spotlight (`mdfind`) | nfind (no flag) | nfind `--macos-meta` |
|---|---|---|---|
| "PDFs I downloaded from the web that mention 'invoice'" | sees the download flag, can't grep PDF text | reads PDF text, can't see quarantine | ✅ both |
| "files tagged Red whose contents contain a TODO" | sees the tag, not the content condition | reads content, not the tag | ✅ both |
| "scripts downloaded from github.com that define `main`" | sees where-from, not the code | parses the code, not where-from | ✅ both |

For **pure**-metadata queries ("everything tagged Red", "files downloaded yesterday"),
prefer Spotlight (`mdfind`) — it answers those instantly from its index. `--macos-meta`
earns its keep only when a content or structure condition is also present. See
[How nfind compares](comparison.md).

## What's exposed

Inside a **Python** filter, a global dict `META` maps each path to its metadata. Only
paths that have metadata appear, so use `META.get(path, {})`:

| Field | Type | Meaning |
|---|---|---|
| `tags` | `list[str]` | Finder tag names, e.g. `["Red", "Work"]`. |
| `quarantined` | `bool` | `True` if the file carries a download (quarantine) flag. |
| `where_froms` | `list[str]` | Source URLs the file was downloaded from. |

A filter the model might generate for *"files tagged Red whose contents contain a
TODO"*:

```python
def filter_paths(paths):
    matches = []
    for p in paths:
        if "Red" not in META.get(p, {}).get("tags", []):
            continue
        try:
            if "TODO" in open(p, encoding="utf-8", errors="ignore").read():
                matches.append(p)
        except (OSError, IsADirectoryError):
            pass
    return matches
```

Use [`--show-code`](cli.md#reviewing-the-generated-code) to see how a given prompt uses
`META`.

## Notes & limits

- **macOS only.** On other platforms the flag is ignored (with a warning) and `META`
  is an empty dict.
- **Python runtime only.** `META` is not provided to the [Node.js runtime](runtimes.md);
  the prompt nudges the model toward Python when metadata is requested.
- **Opt-in by design.** Reading attributes per path adds host-side work, so it only
  runs when you pass the flag.
- **Prototype scope.** Only tags and download provenance are wired up today; other
  attributes (UTI/kind, `birthtime`, …) are deliberately out of scope for now.
