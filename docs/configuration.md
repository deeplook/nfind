# Configuration

pfind has no config file of its own. Everything is controlled through
[command-line options](cli.md#options) and a handful of environment variables. This
page is a single place to see all of them; each links to the doc with the full detail.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | API key for the default OpenAI provider. Required unless you select another provider. |
| `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `MISTRAL_API_KEY`, `DEEPSEEK_API_KEY`, `XAI_API_KEY`, `OPENROUTER_API_KEY` | API key for the matching `provider/model` selector. Only the selected provider's key is needed. See [Providers](cli.md#providers). |
| `PFIND_WHITELIST` | Overrides the path of the approved-package [whitelist file](dependencies.md#the-whitelist-file). |
| `XDG_CONFIG_HOME` | Base directory for the whitelist (`$XDG_CONFIG_HOME/pfind/whitelist.json`); defaults to `~/.config`. |
| `NO_COLOR` | When set, disables colored output and syntax highlighting (the [`NO_COLOR`](https://no-color.org/) convention). Color is also disabled when stderr is not a TTY. |

Local providers (`ollama/…`, `lmstudio/…`) need no API key.

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
