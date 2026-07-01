# Output modes

← [Home](index.md)

By default `nfind` prints one path per line, like `find`. When your prompt asks for
extra per-file information, the generated filter attaches it to each result and you
choose how to surface it.

Internally nfind always works with structured **records** — each result is an object
with at least a `path` key, plus any extra fields the prompt produced. The output
*mode* is just how those records are formatted for display.

## Default — paths only

```bash
nfind "Python files that import os"
```

```
/path/to/a.py
/path/to/b.py
```

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

## Notes

- `--json` and `--fields` are **mutually exclusive** (using both exits with code 2).
- `--extract` and `--fields` are **mutually exclusive**; `--extract` with `--json` keeps
  the nested JSON (the explode applies only to text output).
- Whether extra fields appear depends entirely on the prompt. "Python files" yields
  bare paths; "Python files **and their line count**" yields a `lines` field.
- The field names are chosen by the model from your wording. Ask explicitly (e.g.
  "…with a field named `lines`") if you need a stable schema for scripting.
- From Python, [`search()`](api.md) returns these records directly as a list of dicts.
