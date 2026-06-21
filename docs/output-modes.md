# Output modes

← [Home](index.md)

By default `pfind` prints one path per line, like `find`. When your prompt asks for
extra per-file information, the generated filter attaches it to each result and you
choose how to surface it.

Internally pfind always works with structured **records** — each result is an object
with at least a `path` key, plus any extra fields the prompt produced. The output
*mode* is just how those records are formatted for display.

## Default — paths only

```bash
pfind "Python files that import os"
```

```
/path/to/a.py
/path/to/b.py
```

Clean and pipeable. Any extra fields the filter produced are ignored in this mode.

## `--verbose` / `-v` — path plus fields

```bash
pfind "Python files, and for each the number of lines" --verbose
```

```
/path/to/a.py	lines=42
/path/to/b.py	lines=7
```

Each path is followed by a tab and its extra fields as `key=value`, comma-separated.
When a result has no extra fields, only the path is printed — so `--verbose` degrades
gracefully to the default for prompts that don't ask for data.

## `--json` — machine-readable records

```bash
pfind "Python files, and for each the number of lines" --json
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
pfind "Python files, and for each the number of lines" --json \
  | jq '.results | sort_by(.lines) | reverse | .[0]'
```

## Notes

- `--json` and `--verbose` are **mutually exclusive** (using both exits with code 2).
- Whether extra fields appear depends entirely on the prompt. "Python files" yields
  bare paths; "Python files **and their line count**" yields a `lines` field.
- The field names are chosen by the model from your wording. Ask explicitly (e.g.
  "…with a field named `lines`") if you need a stable schema for scripting.
- From Python, [`search()`](api.md) returns these records directly as a list of dicts.
