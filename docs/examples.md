# Examples

← [Home](index.md)

- [Structure & naming](#structure--naming)
- [Content-aware searches](#content-aware-searches)
- [Project & ecosystem patterns](#project--ecosystem-patterns)
- [With extra data](#with-extra-data)
- [Which runtime and image each prompt uses](#which-runtime-and-image-each-prompt-uses)
- [Piping results](#piping-results)

---

The prompt is free-form — describe what you want in plain language. These are prompts
that work well; adapt them to your needs.

## Structure & naming

```bash
pfind "files that have no extension"
pfind "directories that contain more than 50 files"
pfind "empty directories (no files anywhere beneath them)"
pfind "files nested more than 4 levels deep"
pfind "files whose names contain both a version number and a date"
pfind "directories whose names suggest backups (contain 'backup', 'bak', 'old', or end with ~)"
pfind "files that have a twin with the same stem but a different extension in the same folder"
```

## Content-aware searches

Because the filter runs in the sandbox with the tree mounted read-only, it can read
file *contents*, not just names:

```bash
pfind "Python files that import requests"
pfind "shell scripts that use 'rm -rf'"
pfind "JSON files that are not valid JSON"
pfind "text files that contain a TODO comment"
pfind "__init__.py files that are zero bytes"
```

## Project & ecosystem patterns

```bash
pfind "Python virtual environments (directories with a pyvenv.cfg directly inside)"
pfind "Helm chart directories (contain both Chart.yaml and a templates subdirectory)"
pfind "initialized Terraform root modules (have a .terraform subdirectory and a .tf file)"
pfind "macOS .app bundles (name ends in .app and contains Contents/Info.plist)"
pfind "directories that contain at least one audio file and no files of any other type"
```

## With extra data

When the prompt asks for per-file information, surface it with `--verbose` or
`--json`:

```bash
pfind "Python files, and for each the number of lines" --verbose
# /path/to/a.py	lines=42
# /path/to/b.py	lines=7

pfind "Python files that import pandas, with how many times each uses it" --json
# { "count": 2, "results": [ { "path": "...", "uses": 5 }, … ] }
```

See [Output modes](output-modes.md) for how this works.

## Which runtime and image each prompt uses

The model picks a [runtime](runtimes.md) and declares any packages, which together
determine the sandbox image pfind builds and runs. Prompts that need no packages use
the stdlib-only base image; prompts that need libraries use a cached derived image
(`…:deps-<hash>`).

| Prompt | Runtime | Packages | Image used |
|---|---|---|---|
| `files with no extension` | Python | — | `pfind-search-paths:latest` (base) |
| `directories that contain only audio files` | Python | — | `pfind-search-paths:latest` (base) |
| `MP3 files whose title tag contains 'live', using mutagen` | Python | `mutagen` (pip) | `pfind-search-paths:deps-<hash>` |
| `images larger than 4000px on a side` | Python | `pillow` (pip) | `pfind-search-paths:deps-<hash>` |
| `Go files defining a function named Test*, using tree-sitter` | Python | `tree-sitter`, `tree-sitter-go` (pip) | `pfind-search-paths:deps-<hash>` |
| `PDFs I downloaded that mention 'invoice', using pypdf` (with `--macos-meta`) | Python | `pypdf` (pip) | `pfind-search-paths:deps-<hash>` |
| `TypeScript files that declare an interface, using the node runtime, no packages` | Node.js | — | `pfind-search-node:latest` (base) |
| `TypeScript files that export a default, using ts-morph` | Node.js | `ts-morph` (npm) | `pfind-search-node:deps-<hash>` |

The exact image is chosen automatically; you rarely need to think about it. To see
what a given prompt does:

```bash
# --show-code prints the runtime in its header and the generated source
pfind "TypeScript files that export a default, using ts-morph" ./src --show-code

# list the images pfind has built
docker images 'pfind-search-*'
```

Packages on the per-runtime default list (e.g. `mutagen`, `pillow`, `ts-morph`)
install without prompting; anything else is confirmed and remembered. See
[Dependencies & the whitelist](dependencies.md).

## Piping results

Output is a plain path list on stdout (build logs and the optional `--show-code`
output go to stderr), so results pipe cleanly:

```bash
pfind "files with no extension" | wc -l
pfind "shell scripts that use 'rm -rf'" | xargs -I{} shellcheck {}
pfind "audio files (mp3, flac, wav)" ~/Music > audio.txt
```
