# How pfind compares

← [Home](index.md)

pfind overlaps with tools like **Spotlight** (`mdfind`), classic **`find`/`grep`**, and
**lfind**, but takes a fundamentally different approach: instead of querying an index
or matching fixed predicates, it **generates a small program per question and runs it
live** over the target directory in a sandbox.

> In one line: Spotlight is a pre-built index you query; pfind is a program it writes
> per question and runs now.

## pfind vs. Spotlight (`mdfind`)

| | Spotlight / `mdfind` | pfind |
|---|---|---|
| Mechanism | Background **index** of metadata + content, kept fresh continuously | **No index** — walks the target dir at query time, generates a filter, runs it |
| A query becomes | A lookup against the index (constrained `kMDItem*` predicate grammar) | A natural-language prompt → generated Python/JS, run in a Docker sandbox |
| Speed / scale | Near-instant, whole-disk, ranked | Seconds per query; aimed at a specific directory |
| Freshness | Current via filesystem events | Current — reads the real files now, nothing to go stale |
| Platform | macOS only, built-in, offline | macOS/Linux; needs Docker + an OpenAI key |

## What each is good at

**Spotlight wins at** speed and scale (instant, whole-disk, ranked), full-text content
search of indexed documents, and rich extracted metadata (EXIF, audio bitrate, kind,
dates) — with zero setup and a system-wide UI.

**pfind wins at** questions a fixed attribute index can't express:

- **Structural / relational** queries — "directories containing *only* audio files",
  "Python files larger than their `.pyc`", "Helm charts with both `Chart.yaml` and a
  `templates/`", "initialized Terraform modules".
- **Computed output** — "…and for each, the line count / number of imports"
  ([`--verbose` / `--json`](output-modes.md)). Spotlight returns files, not derived data.
- **On-demand libraries** — pulls in real tooling per query (mutagen for ID3 tags,
  `ts-morph` for TypeScript) via its [runtimes](runtimes.md) and
  [dependencies](dependencies.md).
- **Cross-platform** and scriptable to a clean stdout path list.

## Trade-offs pfind makes

- **Not an index** — it doesn't compete on "search my whole disk instantly"; it targets
  a directory and a structural question.
- **Non-deterministic** — the model picks the implementation, so results can vary; this
  is why [`--show-code` / `--confirm`](cli.md#reviewing-the-generated-code) exist.
- **Dependencies** — needs Docker and an API key, and the host reaches the OpenAI API
  to generate code. Your **prompt** is sent to the model; your **file list and contents
  are not** — the actual file access happens locally in a no-network sandbox. Spotlight
  indexes locally and never leaves the machine.

## The mental model

- Spotlight ≈ search-engine for your disk (indexed, instant, ranked).
- pfind ≈ asking an analyst to write and run a one-off script against a folder, safely.

They're **complementary**: use Spotlight/`mdfind` (or `fd`) to locate by content or
metadata fast; use pfind when the question is about **structure, relationships, or
computed properties** an attribute index can't represent. A fast pre-filter
(`mdfind`/`fd`) feeding pfind's generated logic would also ease pfind's main cost —
re-scanning the tree on every query.
