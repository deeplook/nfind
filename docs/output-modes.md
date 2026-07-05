# Output modes

← [Home](index.md)

By default `nfind` prints one path per line, like `find`. When your prompt asks for
extra per-file information, the generated filter attaches it to each result and you
choose how to surface it.

Internally nfind always works with structured **records** — each result is an object
with at least a `path` key, plus any extra fields the prompt produced. The output
*mode* is just how those records are formatted for display.

## One operation, several renderings

nfind does essentially one thing: **select** a subset of your files and, optionally,
annotate each with extra fields the prompt asked for. Everything on this page —
`--fields`, `--json`, `--extract` — is a *rendering* of that same set of records, not a
different kind of query. The generated filter returns files; the mode decides how they
reach your terminal.

`--extract` is the one addition that goes past whole files. A filter can attach a
**list-valued** field (`todos: [{line, text}, …]`), and `--extract` explodes it into one
row per element — nfind selecting the things *inside* files rather than the files
themselves. It is still the same select-and-annotate contract: `--extract` only renders a
field the filter already returned (see [`--extract`](#--extract--items-inside-files)).

Some questions look adjacent but are deliberately **not** nfind's job — they fall out of a
selection plus an ordinary Unix pipe:

- **Counting or aggregating** ("how many Python files import `requests`", "total size of
  all PDFs") — select with the field, then fold with `wc -l`, `awk`, or `jq`.
- **Answering in prose** ("what test framework does this project use") — a question for a
  chat/code-QA tool, not a file selector.
- **Proposing edits** ("rename these to kebab-case") — select the targets, then act on
  them with `sed`, `mv`, or `xargs`.

nfind stays a *selector* and composes with the tools you already have, rather than growing
a verb for each of these. The examples throughout the docs lean on exactly that: `nfind …
--json | jq`, `nfind … | wc -l`, `nfind … | xargs`.

## Default — paths only

```bash
nfind "Python files that import os"
```

```
/path/to/a.py
/path/to/b.py
```

Result paths are always **absolute and symlink-resolved** (the equivalent of
`realpath`), even when you searched a relative root such as `.`. On macOS this means a
search under `/tmp` prints `/private/tmp/...`, since `/tmp` is a symlink. If you need to
string-match results against paths from another tool, resolve those too (e.g. with
`realpath`) so both sides use the canonical form.

Clean and pipeable. Any extra fields the filter produced are ignored in this mode.

## `--fields` / `-f` — path plus fields

```bash
nfind "Python files, and for each the number of lines" --fields
```

```
/path/to/a.py	lines=42
/path/to/b.py	lines=7
```

Each path is followed by a tab and its extra fields as `key=value`, comma-separated.
When a result has no extra fields, only the path is printed — so `--fields` degrades
gracefully to the default for prompts that don't ask for data.

A **list-valued** field is summarised as its element **count** rather than dumped
(`key=value` can't faithfully render a nested object), so a record carrying
`todos: [{…}, {…}, {…}]` prints `todos=3`. Use [`--extract`](#--extract--items-inside-files)
for one line per element, or `--json` for the full nested record.

## `--json` — machine-readable records

```bash
nfind "Python files, and for each the number of lines" --json
```

```json
{
  "count": 2,
  "results": [
    { "path": "/path/to/a.py", "lines": 42 },
    { "path": "/path/to/b.py", "lines": 7 }
  ]
}
```

`results` is the list of records (host paths plus extra fields); `count` is their
number. Pipe it into `jq` for further processing:

```bash
nfind "Python files, and for each the number of lines" --json \
  | jq '.results | sort_by(.lines) | reverse | .[0]'
```

## `--extract` — items inside files

The other modes render one line per **file**. `--extract` selects the things *inside*
files: when a filter returns a record with a **list-valued** field — say
`todos: [{line, text}, …]` — `--extract` explodes that field, streaming one match per
line instead of one path per line. Passing `--extract` also steers the model to produce
such a field.

```bash
nfind --extract "every TODO comment, with its file and line number" ./src
```

```
/src/app.py:42	handle retry
/src/app.py:91	drop after #312
/src/worker.py:7	make timeout configurable
```

Each row is `path[:line]<TAB>payload`. The `line` (and optional `col`) anchor the path
prefix and appear only when the item is line-anchored; a `text` field becomes the bare
payload, and any other field renders as `key=value`. A scalar element (e.g. a bare URL)
prints verbatim. Because each match is its own line, the stream feeds `wc -l`, `sort`,
`awk`, and friends at **match grain**:

```bash
nfind --extract "every TODO with file and line" ./src | wc -l   # counts matches, not files
nfind --extract "all hardcoded URLs in config files" ./config | sort -u
```

When a record has **more than one** list-valued field, `--extract` refuses to guess and
exits naming the candidates — pick one with `--extract-field NAME`. A record with no list
field degrades to a single path line, so mixed result sets still print. `--extract` works
on the replay path too (`nfind --run filter.py --extract`), since it only *renders* what
the filter returned.

`--json` is unaffected by `--extract`: it always emits the canonical nested record, so
machine consumers see one schema. Flatten to match grain with `jq` when you need it:

```bash
nfind --extract "every TODO, with file and line" ./src --json \
  | jq '[.results[] as $r | $r.todos[] | {path:$r.path} + .]'
```

## Side by side — one query, every mode

Because the modes are just renderings of the same records, the difference is clearest with
a single prompt shown every way. Take a filter that returns each Python file with a
**list-valued** `todos` field — two files, three TODOs in total:

```jsonc
// the records the filter returns
{ "path": "/src/app.py",    "todos": [ {"line": 42, "text": "handle retry"},
                                        {"line": 91, "text": "drop after #312"} ] }
{ "path": "/src/worker.py", "todos": [ {"line":  7, "text": "make timeout configurable"} ] }
```

Run the one prompt with different flags (the prompt is held in `$Q` so only the flag
changes):

```bash
Q="Python files with TODO comments, each with a list of its TODOs (line and text)"
```

**No flags** — paths only; the `todos` field is ignored:

```bash
nfind "$Q" ./src
```
```
/src/app.py
/src/worker.py
```

**`--fields`** — scalars render as `key=value`; a list field collapses to a **count**:

```bash
nfind "$Q" ./src --fields
```
```
/src/app.py	todos=2
/src/worker.py	todos=1
```

**`--json`** — the full nested record, unchanged:

```bash
nfind "$Q" ./src --json
```
```json
{
  "count": 2,
  "results": [
    { "path": "/src/app.py",    "todos": [ { "line": 42, "text": "handle retry" },
                                           { "line": 91, "text": "drop after #312" } ] },
    { "path": "/src/worker.py", "todos": [ { "line": 7, "text": "make timeout configurable" } ] }
  ]
}
```

**`--extract`** — the `todos` list explodes to one row per element (match grain):

```bash
nfind "$Q" ./src --extract
```
```
/src/app.py:42	handle retry
/src/app.py:91	drop after #312
/src/worker.py:7	make timeout configurable
```

**`--extract-field`** — needed only when a record carries **more than one** list field. If
the filter also returns an `imports` list, plain `--extract` can't guess which to explode
and exits:

```bash
nfind "Python files, each with its TODOs and its imported modules" ./src --extract
# error: record for '/src/app.py' has multiple list-valued fields (imports, todos);
#        use --extract-field NAME to choose which one to explode.
```

Name the field to resolve it — the output is then identical to the single-field
`--extract` above:

```bash
nfind "Python files, each with its TODOs and its imported modules" ./src \
  --extract --extract-field todos
```
```
/src/app.py:42	handle retry
/src/app.py:91	drop after #312
/src/worker.py:7	make timeout configurable
```

## Notes

- `--json` and `--fields` are **mutually exclusive** (using both exits with code 2).
- `--extract` and `--fields` are **mutually exclusive**; `--extract` with `--json` keeps
  the nested JSON (the explode applies only to text output).
- Whether extra fields appear depends entirely on the prompt. "Python files" yields
  bare paths; "Python files **and their line count**" yields a `lines` field.
- The field names are chosen by the model from your wording. Ask explicitly (e.g.
  "…with a field named `lines`") if you need a stable schema for scripting.
- From Python, [`search()`](api.md) returns these records directly as a list of dicts.
