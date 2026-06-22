# Configuration

pfind is controlled through [command-line options](cli.md#options), a handful of
environment variables, and an optional [config file](#config-file) that supplies defaults
for the options. This page is a single place to see all of them; each links to the doc
with the full detail.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | API key for the default OpenAI provider. Required unless you select another provider. |
| `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `MISTRAL_API_KEY`, `DEEPSEEK_API_KEY`, `XAI_API_KEY`, `OPENROUTER_API_KEY` | API key for the matching `provider/model` selector. Only the selected provider's key is needed. See [Providers](cli.md#providers). |
| `PFIND_CONFIG` | Path to the [config file](#config-file). Overrides the default location. |
| `PFIND_WHITELIST` | Overrides the path of the approved-package [whitelist file](dependencies.md#the-whitelist-file). |
| `XDG_CONFIG_HOME` | Base directory for the config (`$XDG_CONFIG_HOME/pfind/config.toml`) and whitelist (`…/pfind/whitelist.json`); defaults to `~/.config`. |
| `NO_COLOR` | When set, disables colored output and syntax highlighting (the [`NO_COLOR`](https://no-color.org/) convention). Color is also disabled when stderr is not a TTY. |

Local providers (`ollama/…`, `lmstudio/…`) need no API key.

## Config file

A TOML file can set defaults for the most-used options so you don't repeat them on every
run. pfind reads it from, in order:

1. `--config PATH`,
2. `$PFIND_CONFIG`,
3. `$XDG_CONFIG_HOME/pfind/config.toml` (falling back to `~/.config/pfind/config.toml`).

An explicit `--config`/`PFIND_CONFIG` path must exist; the default location is used only
when present, so no config file is required. **Command-line options always override the
file**, which overrides the built-in defaults.

```toml
# ~/.config/pfind/config.toml
model = "anthropic/claude-3-5-sonnet-latest"
timeout = 30
memory = "512m"
cpus = 2
pids-limit = 128
build-timeout = 180
verbose = true
no-format = false
```

The settable keys mirror the option flag names (the underscore spelling, e.g.
`pids_limit`, is also accepted): `model`, `image`, `timeout`, `memory`, `cpus`,
`pids-limit`, `build-timeout`, `json`, `verbose`, `no-format`. Per-invocation actions
(`--save`, `--run`) and package-approval shortcuts (`--yes`, `--no-deps`) are
intentionally **not** configurable, so each run stays explicit. An unknown key or a
wrong value type is a hard error that names the offending key.

## Selecting a model and provider

The model is chosen with [`--model`](cli.md#options) (default `gpt-4o-mini`). A bare
name uses OpenAI; a `provider/model` selector targets any OpenAI-compatible
[provider](cli.md#providers), for example:

```bash
pfind "large log files" --model anthropic/claude-3-5-sonnet-latest
pfind "TypeScript files using ts-morph" --model ollama/llama3.1
```

## Persistent state

The only state pfind persists between runs is the dependency
[whitelist](dependencies.md#the-whitelist-file) — the packages you've approved for
filters to install. Edit or delete that file to manage what installs without a prompt;
relocate it with `PFIND_WHITELIST`.

## See also

- [CLI reference](cli.md) — every option and argument.
- [Providers](cli.md#providers) — the full provider/selector/key table.
- [Dependencies & the whitelist](dependencies.md) — the approval flow and whitelist file.
