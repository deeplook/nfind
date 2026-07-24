# Query Cache

← [Home](index.md)

- [What it does](#what-it-does)
- [How matching works](#how-matching-works)
- [Semantic matching (opt-in)](#semantic-matching-opt-in)
- [Managing the cache](#managing-the-cache)
- [Options and configuration](#options-and-configuration)
- [Where it lives](#where-it-lives)
- [Privacy](#privacy)

---

## What it does

Most `nfind` prompts are repetitive: the same question, typed again days later. nfind
caches every generated filter next to the prompt that produced it, so re-running a prompt
you have used before **replays the stored filter and skips the LLM call** — the repeat
search is faster and free. The stored prompt/filter pairs also double as a browsable,
auditable history of every filter you have generated.

Caching is **on by default**. The first time you run a prompt it is generated as usual and
saved; the next time it is reused:

```bash
nfind "PDFs modified in the last week" ~/Documents   # generates, runs, and stores
nfind "PDFs modified in the last week" ~/Documents   # cache hit: no LLM call
```

A cache hit prints a short note to stderr, e.g.:

```
reusing cached filter #7 from 2026-07-24T09:15:02; pass --force to regenerate.
```

## How matching works

The default matcher is **normalized-exact** and needs no extra dependencies: the prompt is
lower-cased and its runs of whitespace collapsed, then compared for equality. So these all
hit the same entry:

```
"PDFs modified in the last week"
"pdfs modified in the last week"
"PDFs   modified in the last week"
```

A cached filter is only reused when the **generation-affecting mode** matches too, because
those change the code the model is asked to produce:

- `--macos-meta` (exposes Finder tags / provenance to the filter)
- `--extract` (steers the model to produce a list-valued field)

A prompt run once plainly and once with `--extract` keeps two independent cache entries.

To ignore any cached match and regenerate with the model — the fresh result is still
stored — use `--force`. To skip the cache entirely for a run (no read, no write), use
`--no-cache`.

## Semantic matching (opt-in)

Normalized-exact matching misses prompts that *mean* the same thing but are worded
differently. Semantic matching closes that gap by comparing prompt **embeddings** with a
cosine-nearest-neighbour lookup, so a differently phrased but equivalent prompt still hits.

It is opt-in because it adds a dependency and an embedding call, and it is a **set-once
preference**: enable it and tune it in the [config file](configuration.md) rather than with
per-run flags. Install the extra and turn it on:

```bash
pip install 'nfind[semantic]'
```

```toml
# ~/.config/nfind/config.toml
cache-semantic = true
# cache-embedding-model = "openai/text-embedding-3-small"   # the default
# cache-threshold = 0.15                                    # the default
```

Then ordinary searches reuse semantically similar prompts automatically:

```bash
nfind "PDFs modified in the last week" ~/Documents        # stored with an embedding
nfind "pdf files changed over the past 7 days" ~/Documents   # semantic hit
```

- **`cache-semantic`** turns semantic matching on.
- **`cache-embedding-model`** picks the embedding model as `provider/model`. Embeddings are
  produced through the **same provider that generates filters** — an OpenAI model embeds via
  OpenAI, a local `ollama/…` model embeds locally. Default `openai/text-embedding-3-small`;
  use a local provider to embed fully on-device.
- **`cache-threshold`** is the cosine **distance** cutoff (`0` = identical) for a reuse.
  Default `0.15`; lower is stricter.

The vector index is built for one embedding model. If you change `cache-embedding-model`,
semantic lookups are skipped (comparing across models is meaningless) until you
`nfind cache clear`; normalized-exact matching keeps working. If `cache-semantic` is on
without the extra installed, nfind prints a warning and falls back to exact matching.

## Managing the cache

`nfind cache` is a small subcommand group for inspecting and clearing stored entries. The
default `nfind "prompt"` interface is unchanged.

```bash
nfind cache list             # every stored prompt, newest first (id, date, model, prompt)
nfind cache show 7           # one entry: prompt, provenance, and the generated filter code
nfind cache delete 7         # delete one entry by id (accepts several ids)
nfind cache clear            # delete all entries (asks for confirmation)
nfind cache clear --yes      # delete all entries without confirmation
```

Entry ids are **stable identifiers, not positions**: deleting one leaves a gap rather than
renumbering the rest, so an id you noted (or scripted) always refers to the same entry.
`nfind cache delete 3 7 12` removes several at once and ignores ids that don't exist.

`cache` and [`config`](configuration.md#managing-configuration-from-the-cli) are nfind's
subcommands; the default `nfind "prompt"` interface is unchanged. In the rare case a prompt
collides with a subcommand name, use the explicit `nfind search "prompt"` form.

## Options and configuration

Two per-run flags live on the search command; the semantic settings are configuration-only
(set once in the config file), keeping the main `nfind` option list uncluttered.

| Setting | CLI flag | Config key | Default | Meaning |
| --- | --- | --- | --- | --- |
| Use the cache | `--cache` / `--no-cache` | `cache` | on | Read from and write to the cache. |
| Regenerate now | `--force` | — | off | Ignore a cached match and regenerate (still stored). |
| Semantic matching | — | `cache-semantic` | off | Also reuse semantically similar prompts. |
| Embedding model | — | `cache-embedding-model` | `openai/text-embedding-3-small` | Embedding model for semantic matching. |
| Reuse threshold | — | `cache-threshold` | `0.15` | Max cosine distance for a semantic reuse. |

Config-file example (see [Configuration](configuration.md)):

```toml
cache = true
cache-semantic = true
cache-embedding-model = "ollama/nomic-embed-text"
cache-threshold = 0.2
```

## Where it lives

The cache is a single SQLite file in nfind's per-user cache directory as `queries.db`
(for example `~/.cache/nfind/queries.db` on Linux/macOS). Set `$NFIND_QUERY_CACHE` to point
it elsewhere. Delete the file, or run `nfind cache clear`, to reset.

## Privacy

The cache changes nothing about nfind's privacy model. The generated filter runs in the
[sandbox](safety.md), and your **file contents and paths never leave the machine**. The
only thing sent anywhere is the prompt text — and only to generate (or, with semantic
matching enabled, embed) the filter, which is exactly the request nfind already makes today.
Embedding through a local `ollama/…` model keeps even that on-device.
