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
nfind "files that have no extension"
nfind "directories that contain more than 50 files"
nfind "empty directories (no files anywhere beneath them)"
nfind "files nested more than 4 levels deep"
nfind "files whose names contain both a version number and a date"
nfind "directories whose names suggest backups (contain 'backup', 'bak', 'old', or end with ~)"
nfind "files that have a twin with the same stem but a different extension in the same folder"
```

## Content-aware searches

Because the filter runs in the sandbox with the tree mounted read-only, it can read
file *contents*, not just names:

```bash
nfind "Python files that import requests"
nfind "shell scripts that use 'rm -rf'"
nfind "JSON files that are not valid JSON"
nfind "text files that contain a TODO comment"
nfind "__init__.py files that are zero bytes"
```

## Project & ecosystem patterns

```bash
nfind "Python virtual environments (directories with a pyvenv.cfg directly inside)"
nfind "Helm chart directories (contain both Chart.yaml and a templates subdirectory)"
nfind "initialized Terraform root modules (have a .terraform subdirectory and a .tf file)"
nfind "macOS .app bundles (name ends in .app and contains Contents/Info.plist)"
nfind "directories that contain at least one audio file and no files of any other type"
```

Source structure is fair game too — these parse the code with a bundled tree-sitter
grammar (no network needed). The `.tsx` example uses `tree-sitter-typescript`, whose
grammar is selected with `language_tsx()`:

```bash
nfind "regular .tsx files (skip directories) that define a React function component returning JSX, using tree-sitter-typescript"
nfind "regular .ts files (skip directories) that declare an exported interface, using tree-sitter-typescript"
nfind "Kotlin files that declare a data class, using tree-sitter-kotlin"
```

## With extra data

When the prompt asks for per-file information, surface it with `--fields` or
`--json`:

```bash
nfind "Python files, and for each the number of lines" --fields
# /path/to/a.py	lines=42
# /path/to/b.py	lines=7

nfind "Python files that import pandas, with how many times each uses it" --json
# { "count": 2, "results": [ { "path": "...", "uses": 5 }, … ] }
```

See [Output modes](output-modes.md) for how this works.

## Which runtime and image each prompt uses

The model picks a [runtime](runtimes.md) and declares any packages, which together
determine the sandbox image nfind builds and runs. Prompts that need no packages use
the stdlib-only base image; prompts that need libraries use a cached derived image
(`…:deps-<hash>`).

| Prompt | Runtime | Packages | Image used |
|---|---|---|---|
| `files with no extension` | Python | — | `nfind-search-paths:latest` (base) |
| `directories that contain only audio files` | Python | — | `nfind-search-paths:latest` (base) |
| `MP3 files whose title tag contains 'live', using mutagen` | Python | `mutagen` (pip) | `nfind-search-paths:deps-<hash>` |
| `images larger than 4000px on a side` | Python | `pillow` (pip) | `nfind-search-paths:deps-<hash>` |
| `Go files defining a function named Test*, using tree-sitter` | Python | `tree-sitter`, `tree-sitter-go` (pip) | `nfind-search-paths:deps-<hash>` |
| `.tsx files that define a React function component, using tree-sitter-typescript` | Python | `tree-sitter`, `tree-sitter-typescript` (pip) | `nfind-search-paths:deps-<hash>` |
| `PDFs I downloaded that mention 'invoice', using pypdf` (with `--macos-meta`) | Python | `pypdf` (pip) | `nfind-search-paths:deps-<hash>` |
| `TypeScript files that declare an interface, using the node runtime, no packages` | Node.js | — | `nfind-search-node:latest` (base) |
| `TypeScript files that export a default, using ts-morph` | Node.js | `ts-morph` (npm) | `nfind-search-node:deps-<hash>` |

The exact image is chosen automatically; you rarely need to think about it. To see
what a given prompt does:

```bash
# --show-code prints the runtime in its header and the generated source
nfind "TypeScript files that export a default, using ts-morph" ./src --show-code

# list the images nfind has built
docker images 'nfind-search-*'
```

Packages on the per-runtime default list (e.g. `mutagen`, `pillow`, `ts-morph`)
install without prompting; anything else is confirmed and remembered. See
[Dependencies & the whitelist](dependencies.md).

## Piping results

Output is a plain path list on stdout (build logs and the optional `--show-code`
output go to stderr), so results pipe cleanly:

```bash
nfind "files with no extension" | wc -l
nfind "shell scripts that use 'rm -rf'" | xargs -I{} shellcheck {}
nfind "audio files (mp3, flac, wav)" ~/Music > audio.txt
```

nfind can also read its roots *from* stdin with `-` (newline- or NUL-delimited,
auto-detected), so it works as a filter mid-pipeline — let a cheap tool narrow the tree
first, then nfind parse only the survivors:

```bash
find . -name '*.sh' -print0 | nfind "shell scripts that use 'rm -rf'" -
git ls-files '*.py' | nfind "Python files that call eval()" -
```
