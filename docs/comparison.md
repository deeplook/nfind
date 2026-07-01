# How nfind compares

← [Home](index.md)

nfind overlaps with tools like **Spotlight** (`mdfind`), classic **`find`/`grep`**, and
**lfind**, but takes a fundamentally different approach: instead of querying an index
or matching fixed predicates, it **generates a small program per question and runs it
live** over the target directory in a sandbox.

> In one line: Spotlight is a pre-built index you query; nfind is a program it writes
> per question and runs now.

## nfind vs. Spotlight (`mdfind`)

| | Spotlight / `mdfind` | nfind |
|---|---|---|
| Mechanism | Background **index** of metadata + content, kept fresh continuously | **No index** — walks the target dir at query time, generates a filter, runs it |
| A query becomes | A lookup against the index (constrained `kMDItem*` predicate grammar) | A natural-language prompt → generated Python/JS, run in a container sandbox |
| Speed / scale | Near-instant, whole-disk, ranked | Seconds per query; aimed at a specific directory |
| Freshness | Current via filesystem events | Current — reads the real files now, nothing to go stale |
| Platform | macOS only, built-in, offline | macOS/Linux; needs Docker (default) or experimental Apple Containers on macOS + an OpenAI key |

## What each is good at

**Spotlight wins at** speed and scale (instant, whole-disk, ranked), full-text content
search of indexed documents, and rich extracted metadata (EXIF, audio bitrate, kind,
dates) — with zero setup and a system-wide UI.

**nfind wins at** questions a fixed attribute index can't express:

- **Structural / relational** queries — "directories containing *only* audio files",
  "Python files larger than their `.pyc`", "Helm charts with both `Chart.yaml` and a
  `templates/`", "initialized Terraform modules".
- **Computed output** — "…and for each, the line count / number of imports"
  ([`--fields` / `--json`](output-modes.md)). Spotlight returns files, not derived data.
- **On-demand libraries** — pulls in real tooling per query (mutagen for ID3 tags,
  `ts-morph` for TypeScript) via its [runtimes](runtimes.md) and
  [dependencies](dependencies.md).
- **Cross-platform** and scriptable to a clean stdout path list.
- **macOS metadata × content** — with [`--macos-meta`](macos-metadata.md), filters can
  combine Finder tags and download provenance with content/structure conditions (e.g.
  "PDFs I downloaded that mention 'invoice'") — the one place nfind reaches into the
  same metadata Spotlight indexes, to ask questions the index alone can't answer.

## Trade-offs nfind makes

- **Not an index** — it doesn't compete on "search my whole disk instantly"; it targets
  a directory and a structural question.
- **Non-deterministic** — the model picks the implementation, so results can vary; this
  is why [`--show-code` / `--confirm`](cli.md#reviewing-the-generated-code) exist.
- **Dependencies** — needs a container backend and an API key, and the host reaches the OpenAI API
  to generate code. Your **prompt** is sent to the model; your **file list and contents
  are not** — the actual file access happens locally in a read-only sandbox. With the
  default Docker backend, that sandbox has no network; Apple Containers on macOS 15 has
  a weaker networking guarantee. Spotlight indexes locally and never leaves the
  machine.

## Other tools in the space

Beyond Spotlight, nfind brushes up against several tool categories — but it's the only
one that combines **natural language**, **a real generated program** (not a one-liner),
and **local sandboxed execution that reads file contents**. Each neighbour has only part
of that:

| Tool category | Natural language | Reads contents / structure | Runs locally |
| --- | :---: | :---: | :---: |
| `find` / `fd` / `ripgrep`, Spotlight (`mdfind`) | ✗ | partial | ✓ |
| `fselect` / osquery (SQL over files) | ✗ | ✓ | ✓ |
| NL→command helpers (`sgpt`, `gh copilot suggest`) | ✓ | ✗ (just a one-liner) | ✓ |
| Send-the-file-list-to-an-LLM tools (e.g. lfind) | ✓ | ✗ (filenames only) | ✗ |
| NL→code runners ([Open Interpreter](https://github.com/OpenInterpreter/open-interpreter), Code Interpreter) | ✓ | ✓ | partial / not sandboxed-by-default |
| **nfind** | **✓** | **✓** | **✓ (hardened sandbox)** |

- **`fselect` / osquery** answer nfind's *structural* questions deterministically (SQL
  like `select name from /path where size gt 1mb`) — no API key, no LLM — but you write
  the query in their grammar, and they can't read content semantically.
- **NL→command helpers** (`sgpt`, `aichat`, `gh copilot suggest`, Warp AI) turn a prompt
  into a `find`/`fd` one-liner you review and run. Lower fidelity than a generated
  program, and no sandbox.
- **NL→code runners** (Open Interpreter; ChatGPT Code Interpreter / Claude's analysis
  tool) share nfind's "write a program per question, then run it" model, but they're
  general-purpose task runners — not filesystem search — and aren't built around a
  read-only, no-network sandbox over your live tree.
- **lfind** is the closest goal-mate (find files by description) but sends the file list
  to the model and filters by name; it doesn't scale and never sees file contents.

## The mental model

- Spotlight ≈ search-engine for your disk (indexed, instant, ranked).
- nfind ≈ asking an analyst to write and run a one-off script against a folder, safely.

They're **complementary**: use Spotlight/`mdfind` (or `fd`) to locate by content or
metadata fast; use nfind when the question is about **structure, relationships, or
computed properties** an attribute index can't represent. A fast pre-filter
(`mdfind`/`fd`) feeding nfind's generated logic would also ease nfind's main cost —
re-scanning the tree on every query.
