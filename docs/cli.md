# CLI Reference

← [Home](index.md)

- [Synopsis](#synopsis)
- [Arguments](#arguments)
- [Options](#options)
- [Reviewing the generated code](#reviewing-the-generated-code)
- [Output modes](#output-modes)
- [Dependencies](#dependencies)
- [Exit codes](#exit-codes)

---

## Synopsis

```bash
pfind PROMPT [PATH] [OPTIONS]
```

Search `PATH` for files and directories matching the natural-language `PROMPT` and
print one path per line. Both `-h` and `--help` show usage.

```bash
pfind "directories that contain only audio files"
pfind "Python files that import requests" ./src
pfind "files larger than 1 MB, with their size" --verbose
```

## Arguments

| Argument | Default | Description |
|---|---|---|
| `PROMPT` | — (required) | Natural-language description of the paths to find. |
| `PATH` | `.` | Directory to search. |

## Options

| Option | Default | Description |
|---|---|---|
| `--model` | `gpt-4o-mini` | Model used to generate the filter. Bare name = OpenAI; `provider/model` for others (see [Providers](#providers)). |
| `--image` | per-runtime | Override the base image tag for the chosen [runtime](runtimes.md). |
| `--timeout` | `10.0` | Seconds the generated filter may run before it is killed. |
| `--memory` | `256m` | Memory limit for the worker container. |
| `--cpus` | `1.0` | CPU limit for the worker container. |
| `--pids-limit` | `64` | Maximum number of processes inside the worker container. |
| `--rebuild` | off | Rebuild the worker image before searching. |
| `--build-timeout` | `120.0` | Seconds allowed for building the worker image. |
| `--show-code` | off | Print the generated filter (to stderr) before running it. |
| `--save PATH` | — | Write the generated filter code to a file. |
| `--confirm`, `-i` | off | Show the generated code and ask for confirmation before running it. |
| `--verbose`, `-v` | off | Show extra per-path fields alongside each path. |
| `--json` | off | Output results as JSON (path plus any extra fields). |
| `--yes`, `-y` | off | Approve any requested packages without prompting. |
| `--no-deps` | off | Reject any third-party packages (standard library only). |
| `--macos-meta` | off | macOS only: expose Finder tags and download metadata to the filter (see [macOS metadata](macos-metadata.md)). |
| `-h`, `--help` | — | Show help and exit. |

## Reviewing the generated code

The filter is written by an LLM, so you may want to see it before it runs:

```bash
# Print the generated filter (to stderr) before running it
pfind "files with no extension" --show-code

# Save the generated filter to a file
pfind "files with no extension" --save filter.py

# Show the code and ask for confirmation before running (aborts on "no")
pfind "files with no extension" -i        # or --confirm
```

The code is printed to **stderr**, so stdout stays a clean, pipeable list of paths
even with `--show-code`. On a terminal the code is syntax-highlighted with Pygments;
highlighting is disabled when [`NO_COLOR`](https://no-color.org/) is set or when
stderr is redirected.

Declining a `--confirm` prompt aborts before the container runs and exits with code
130.

## Output modes

```bash
pfind "Python files that import os"                          # default: paths only
pfind "Python files, and for each the number of lines" -v   # path + extra fields
pfind "Python files, and for each the number of lines" --json
```

`--json` and `--verbose` are mutually exclusive. See [Output modes](output-modes.md)
for details and example output.

## Dependencies

When a prompt needs a library (e.g. reading MP3 tags), the generated filter declares
the PyPI packages it imports. Packages on the approved list install without a prompt;
new ones are confirmed and then remembered:

```bash
pfind "MP3 files whose title tag contains 'live', using mutagen" ~/Music   # prompts if new
pfind "images larger than 4000px on a side" ~/Photos --yes                 # approve without asking
pfind "files containing TODO" . --no-deps                                  # stdlib only
```

`--yes` and `--no-deps` are mutually exclusive. See
[Dependencies & the whitelist](dependencies.md) for the approval flow, the default
package list, and the whitelist file.

## Providers

`--model` selects the model that writes the filter. A bare name uses OpenAI (so
existing usage is unchanged); a `provider/model` selector targets any
OpenAI-compatible provider — pfind reuses the OpenAI SDK against the provider's
base URL, so no extra dependency is needed.

```bash
pfind "files with no extension"                                   # OpenAI (default)
pfind "..." --model anthropic/claude-sonnet-4-6                   # Anthropic
pfind "..." --model gemini/gemini-2.5-flash                       # Google Gemini
pfind "..." --model groq/llama-3.3-70b-versatile                  # Groq
pfind "..." --model openrouter/<vendor>/<model>                   # OpenRouter (near-universal)
pfind "..." --model ollama/llama3.1                               # local Ollama
```

| Provider | Selector prefix | API key env var |
|---|---|---|
| OpenAI | *(bare name)* or `openai/` | `OPENAI_API_KEY` |
| Anthropic | `anthropic/` | `ANTHROPIC_API_KEY` |
| Google Gemini | `gemini/` | `GEMINI_API_KEY` |
| Groq | `groq/` | `GROQ_API_KEY` |
| Mistral | `mistral/` | `MISTRAL_API_KEY` |
| DeepSeek | `deepseek/` | `DEEPSEEK_API_KEY` |
| xAI (Grok) | `xai/` | `XAI_API_KEY` |
| OpenRouter | `openrouter/` | `OPENROUTER_API_KEY` |
| Ollama (local) | `ollama/` | *(none; needs a running server)* |
| LM Studio (local) | `lmstudio/` | *(none; needs a running server)* |

Only the selected provider's key is needed. Providers vary in whether they support
strict JSON mode; pfind drops it automatically and recovers the JSON from the reply
when a provider doesn't, so generation still works. Some non-OpenAI models follow the
filter contract less reliably — if a model misbehaves, try a stronger one or route
through `openrouter/`.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Search completed (zero or more matches). |
| `1` | A runtime error occurred (e.g. Docker unavailable, filter failed). Message on stderr, prefixed `error:`. |
| `2` | Invalid usage (e.g. `--json` and `--verbose` together). |
| `130` | A `--confirm` prompt was declined. |
