# Runtimes (Python & Node.js)

← [Home](index.md)

Most filters are best written in Python, but some questions are far easier in the
JavaScript/TypeScript ecosystem — parsing TypeScript with `ts-morph`, walking a JS
AST with `acorn`, and so on. pfind supports both: the model **chooses the runtime**
for each prompt, and pfind runs the filter in the matching sandbox image.

## How the runtime is chosen

The model returns a `runtime` alongside the code and dependencies:

- **`python`** (the default) — a `filter_paths(paths)` function; dependencies are pip
  packages; runs in the Python base image (`pfind-search-paths:latest`).
- **`node`** — a CommonJS `filterPaths(paths)` function using `require(...)`;
  dependencies are npm packages; runs in the Node.js base image
  (`pfind-search-node:latest`, based on `node:22-slim`).

It picks `node` only when the JS/TS ecosystem is clearly the better tool; otherwise it
stays with Python. You can nudge it in the prompt ("…using ts-morph", "use the node
runtime").

Everything else is identical across runtimes: the same read-only `/data` mount, no
network, dropped capabilities, resource limits, the same result shapes (a list of
paths, or objects with a `path` field plus extra data), and the same
[output modes](output-modes.md). The host validates that every returned path was one
it supplied, regardless of runtime.

## Dependencies per runtime

Each runtime has its **own** approved-package list and its own section in the
[whitelist file](dependencies.md#the-whitelist-file). Approving `ts-morph` for Node
does not affect Python, and vice versa.

Pre-approved Node packages (install without a prompt):

`@babel/parser`, `acorn`, `esprima`, `fast-xml-parser`, `ts-morph`, `typescript`,
`yaml`

npm packages are installed into a derived Node image
(`pfind-search-node:deps-<hash>`) with `npm install`, exactly like pip packages are
layered onto the Python base. See [Dependencies & the whitelist](dependencies.md).

## Examples

```bash
# Likely Python (default)
pfind "files with no extension"

# Nudge Node + a pre-approved package
pfind "TypeScript files that export a default, using ts-morph" ./src

# Node, standard library only
pfind "TypeScript files that declare an interface, using the node runtime, no packages" ./src
```

Use [`--show-code`](cli.md#reviewing-the-generated-code) to see which runtime was
chosen and the generated source (Python or JavaScript, syntax-highlighted
accordingly).

## Why these two runtimes (and what about others)

A new runtime is real, ongoing surface area: another Dockerfile, an in-container
worker, a package-manager integration, a default whitelist to curate, validation, and
another base image to pull — plus one more choice the model can get wrong. So the bar
for adding one is deliberately high.

**The bar:** a runtime earns its place only when its *native toolchain* provides
analysis that neither existing runtime can match. The decisive distinction is
**syntactic** versus **semantic**:

- **Syntactic** questions — "functions named `Test*`", "files importing package Y",
  "classes with no methods" — need only a parser, not the language itself. The Python
  runtime already covers these for *many* languages via `tree-sitter` and
  `tree-sitter-language-pack` (both pre-approved; see
  [Dependencies](dependencies.md)). Adding a Go or Ruby runtime *just* to parse Go or
  Ruby source would duplicate what tree-sitter already does from Python.
- **Semantic** questions — type resolution, symbol/binding resolution, macro
  expansion — need the language's own compiler. This is exactly why **Node.js** exists
  here: `ts-morph` / the TypeScript compiler API give *type-aware* analysis of TS/JS
  that no syntactic parser can replicate.

Between them, **Python** (a deep standard library, the broadest analysis ecosystem,
and tree-sitter for cross-language *structure*) and **Node.js** (type-aware TS/JS)
cover essentially the whole practical space of file-search queries. That is why the
model is told to pick Node only when the JS/TS ecosystem is *clearly* better, and to
prefer Python otherwise.

### Languages that don't clear the bar

- **Compiled languages (Rust, Java, C)** fight pfind's model: it *generates code and
  runs it immediately* in a disposable container. A per-filter compile step is slow
  and needs a heavy toolchain image. `syn` is excellent for Rust ASTs, but a
  `cargo build` per query is the wrong shape.
- **Interpreted niche languages (Ruby, PHP, Perl)** execute fine but fail the bar:
  tree-sitter already covers their *syntax*, and there's little demand for the
  *semantic* analysis only their own runtime could add.

### The one candidate worth considering: Go

Go is the least-bad next runtime, for the same reason Node is justified: its standard
library ships first-class **semantic** tooling — `go/parser`, `go/types` — that gives
type- and symbol-aware analysis of Go source which tree-sitter (syntactic only) can't.
And `go run` is fast enough to keep the generate-then-run model viable, unlike the
compiled languages above. Even so, it would only be worth adding on real demand for
deep Go-codebase queries — not preemptively. Until then, tree-sitter from the Python
runtime handles structural Go questions well enough.

## Overriding the base image

`--image` overrides the base image tag for whichever runtime the model selects (an
advanced option — the default per-runtime tags are usually what you want).
