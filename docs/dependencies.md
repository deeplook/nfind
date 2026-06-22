# Dependencies & the whitelist

← [Home](index.md)

Some questions can't be answered from names and raw bytes alone — reading MP3 tags,
image dimensions, PDF text, or the structure of source code needs a library. nfind
lets the generated filter **declare the third-party packages it needs**, then installs
them into a sandboxed image — but only after the package has been approved.

The defaults include `tree-sitter` plus a set of per-language grammar wheels
(`tree-sitter-python`, `-javascript`, `-typescript`, `-go`, `-rust`, `-java`, `-c`,
`-bash`, `-kotlin`, `-swift`, `-dart`), so a Python filter can parse source
*structure* — functions, imports,
classes — without a dedicated runtime. Each wheel bundles its compiled grammar, so it
works in the no-network, read-only sandbox; the generated code uses the standard API,
`Parser(Language(tree_sitter_python.language()))`. Reach for the
[Node.js runtime](runtimes.md) only when you need type-aware TypeScript/JS analysis
that the compiler API provides.

## How it works

1. **Declare.** When generating the filter, the model also returns the PyPI packages
   the code imports (for example `mutagen` to read audio tags).
2. **Check the whitelist.** nfind compares the requested packages against an approved
   set for that [runtime](runtimes.md): a small [built-in default list](#the-default-list)
   plus anything you've approved before (saved to disk). Python (pip) and Node.js
   (npm) packages are tracked separately.
3. **Approve new packages.** If a package isn't already approved, nfind asks before
   installing it. On approval it is **remembered** so you're not asked again.
4. **Build a derived image.** Approved packages are installed into a derived worker
   image (`nfind-search-paths:deps-<hash>`) layered on the base. The image is cached
   and reused for the same set of packages. Prompts that need no packages keep using
   the stdlib-only base image.
5. **Run.** The filter executes in the derived image — with the packages available,
   but still no network, read-only mount, and dropped capabilities at run time.

> Installing packages happens at **image build time**, which needs network access.
> The container that runs the filter still has networking disabled.

## Controlling approval

| Flag | Effect |
|---|---|
| *(none)* | Prompt to install any package not already approved; remember approvals. |
| `--yes`, `-y` | Approve and remember any requested packages without prompting. |
| `--no-deps` | Refuse any third-party package — the filter must use the standard library only. |

```bash
# Prompt before installing anything new (default)
nfind "MP3 files whose title tag contains 'live', using mutagen" ~/Music

# Trust this run — install whatever it asks for, and remember it
nfind "images larger than 4000px on either side" ~/Photos --yes

# Force standard-library-only; reject any package request
nfind "files containing the word TODO" . --no-deps
```

If a filter needs a package that isn't approved and you don't approve it (or you pass
`--no-deps`), nfind aborts with a `DependencyError` before building or running
anything.

## The default list

These common, read-only analysis packages are pre-approved and install without a
prompt.

**Python (pip):** `chardet`, `mutagen`, `pdfminer-six`, `pillow`, `pillow-heif`,
`pypdf`, `python-magic`, `pyyaml`, `tinytag`, `tomli`, `tree-sitter`,
`tree-sitter-bash`, `tree-sitter-c`, `tree-sitter-dart`, `tree-sitter-go`,
`tree-sitter-java`, `tree-sitter-javascript`, `tree-sitter-kotlin`,
`tree-sitter-python`, `tree-sitter-rust`, `tree-sitter-swift`,
`tree-sitter-typescript`

**Node.js (npm):** `@babel/parser`, `acorn`, `esprima`, `fast-xml-parser`, `ts-morph`,
`typescript`, `yaml`

## The whitelist file

Approvals are stored as JSON at:

```
$XDG_CONFIG_HOME/nfind/whitelist.json     # or ~/.config/nfind/whitelist.json
```

Override the location with the `NFIND_WHITELIST` environment variable. The file lists
the packages you've approved, per runtime; edit or delete it to manage what installs
without a prompt:

```json
{
  "python": ["rarfile"],
  "node": ["left-pad"]
}
```

The effective allow-set for a runtime is always its built-in
[default list](#the-default-list) **plus** that runtime's entry in this file. (A
legacy flat `{"packages": [...]}` file is still read as the Python list.)

## Why a whitelist

Even sandboxed, installing arbitrary packages carries risk — a package can run code
during installation or pull in unexpected transitive dependencies. Restricting
installs to a vetted, remembered set keeps you in control of what enters the image,
while still letting the filter use real libraries when you allow it. See the
[Safety model](safety.md) for the full picture.
