# Configuration

nfind is controlled through [command-line options](cli.md#options), a handful of
environment variables, and an optional [config file](#config-file) that supplies defaults
for the options. This page is a single place to see all of them; each links to the doc
with the full detail.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | API key for the default OpenAI provider. Required unless you select another provider. |
| `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `MISTRAL_API_KEY`, `DEEPSEEK_API_KEY`, `XAI_API_KEY`, `OPENROUTER_API_KEY` | API key for the matching `provider/model` selector. Only the selected provider's key is needed. See [Providers](cli.md#providers). |
| `NFIND_CONFIG` | Path to the [config file](#config-file). Overrides the default location. |
| `NFIND_WHITELIST` | Overrides the path of the approved-package [whitelist file](dependencies.md#the-whitelist-file). |
| `NFIND_ENDPOINT_CACHE` | Overrides the path of the best-effort model endpoint cache (`chat/completions` vs. `responses`). |
| `XDG_CONFIG_HOME` | Base directory for the config (`$XDG_CONFIG_HOME/nfind/config.toml`) and whitelist (`…/nfind/whitelist.json`); defaults to `~/.config`. Unix only — on Windows, `%APPDATA%\nfind` is used instead. |
| `XDG_CACHE_HOME` | Base directory for the endpoint cache (`$XDG_CACHE_HOME/nfind/model-endpoints.json`); defaults to `~/.cache`. Unix only — on Windows, `%LOCALAPPDATA%\nfind` is used instead. |
| `NO_COLOR` | When set, disables colored output and syntax highlighting (the [`NO_COLOR`](https://no-color.org/) convention). Color is also disabled when stderr is not a TTY. |

Local providers (`ollama/…`, `lmstudio/…`) need no API key.

## Config file

A TOML file can set defaults for the most-used options so you don't repeat them on every
run. nfind reads it from, in order:

1. `--config PATH`,
2. `$NFIND_CONFIG`,
3. `$XDG_CONFIG_HOME/nfind/config.toml` (falling back to `~/.config/nfind/config.toml`;
   on Windows, `%APPDATA%\nfind\config.toml`).

An explicit `--config`/`NFIND_CONFIG` path must exist; the default location is used only
when present, so no config file is required. **Command-line options always override the
file**, which overrides the built-in defaults.

```toml
# ~/.config/nfind/config.toml
model = "anthropic/claude-sonnet-4-6"
sandbox = "docker"
timeout = 30
memory = "512m"
cpus = 2
pids-limit = 128
build-timeout = 180
fields = true
no-format = false
```

The settable keys mirror the option flag names (the underscore spelling, e.g.
`pids_limit`, is also accepted): `model`, `sandbox` (`docker`, `apple`, `podman`, or
`nerdctl`),
`image`, `timeout`, `command-timeout`, `memory`, `cpus`, `pids-limit`, `build-timeout`,
`json`, `fields`, `no-format`, `exclude` (a list of globs), `no-ignore`, `max-depth`,
`max-results`, `max-items`, `max-output-bytes`, and `print0`.
Per-invocation actions (`--save`, `--run`) and package-approval shortcuts (`--yes`,
`--no-deps`) are intentionally **not** configurable, so each run stays explicit. An
unknown key or a wrong value type is a hard error that names the offending key.

```toml
# Enumeration defaults also work, e.g. always skip vendored code:
exclude = ["vendor", "*.min.js"]
max-depth = 6
```

## Selecting a model and provider

The model is chosen with [`--model`](cli.md#options) (default `openai/gpt-5.4`). A bare
name uses OpenAI; a `provider/model` selector targets any OpenAI-compatible
[provider](cli.md#providers), for example:

```bash
nfind "large log files" --model anthropic/claude-sonnet-4-6
nfind "TypeScript files using ts-morph" --model ollama/llama3.1
```

!!! tip "Favour a capable model — it's the cheapest place to spend quality"

    The model does one thing: turn your prompt into the filter *program*. The whole
    correctness of a search rides on getting that code right, so **model quality matters
    here more than almost anywhere**. Yet the call is tiny — a short prompt in, a small
    filter out (your file list and contents are *never* sent) — so even a top-tier model
    usually costs a fraction of a cent per query. And the output is reusable: `--save` the
    generated filter once, then `--run` it as many times as you like with **no further LLM
    calls**. Pay once for a strong model to write good, reusable code; a weaker model may
    only save a fraction of a cent while needing retries or producing a subtly wrong
    filter.

## Persistent state

nfind persists two small files between runs:

- The dependency [whitelist](dependencies.md#the-whitelist-file) — packages you've
  approved for filters to install. Edit or delete that file to manage what installs
  without a prompt; relocate it with `NFIND_WHITELIST`.
- A best-effort model endpoint cache (`model-endpoints.json`) that remembers when a model
  needs OpenAI's `/responses` endpoint instead of `/chat/completions`. Delete it any time;
  nfind will re-probe. Relocate it with `NFIND_ENDPOINT_CACHE`.

## See also

- [CLI reference](cli.md) — every option and argument.
- [Providers](cli.md#providers) — the full provider/selector/key table.
- [Dependencies & the whitelist](dependencies.md) — the approval flow and whitelist file.
