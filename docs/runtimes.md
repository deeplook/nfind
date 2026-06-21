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

## Overriding the base image

`--image` overrides the base image tag for whichever runtime the model selects (an
advanced option — the default per-runtime tags are usually what you want).
