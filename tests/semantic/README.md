# Semantic evaluations

This directory contains 20 deterministic filesystem cases, each with three equivalent
prompts and one committed expected result set. Ordinary `pytest` validates the corpus but
never calls a model.

## Free planning

These targets show the request count and selected model without making API requests:

```sh
make semantic-eval       # 20 canonical prompts
make semantic-eval-all   # all 60 prompt variants
```

## Paid execution

The `-run` targets explicitly enable model calls:

```sh
make semantic-eval-run       # 20 canonical prompts
make semantic-eval-run-all   # all 60 prompt variants
```

The stated counts exclude retries. A generation rejected by nfind's validation may result
in additional requests.

Choose a model through `EVAL_ARGS` using nfind's normal `provider/model` notation:

```sh
make semantic-eval-run \
  EVAL_ARGS="--model openai/gpt-5.4-mini --output eval-report.json"

make semantic-eval-run-all \
  EVAL_ARGS="--model openrouter/vendor/model-name --output eval-report.json"
```

Set the corresponding provider API key exactly as for the `nfind` CLI. The selected sandbox
backend must also be available.

Start with one case when checking a new model or provider:

```sh
make semantic-eval-run \
  EVAL_ARGS="--model openai/gpt-5.4-mini \
  --case python-files-importing-requests \
  --output smoke-report.json"
```

`--case` is repeatable. Pass `--sandbox podman` (or another supported backend) through
`EVAL_ARGS` when Docker is not the intended execution adapter.

## Reports and exit status

Without `--output`, the JSON report is written to stdout. A report contains the model,
sandbox, aggregate exact-match score, and each prompt's expected paths, actual paths,
duration, generated code, selected runtime, dependencies, retry count, and error if any.
The current nfind generation interface does not expose provider token usage, so reports do
not claim token or monetary cost. The evaluator exits with status 1 if one or more evaluations
fail, making it suitable for an explicitly configured scheduled job.

For the direct runner interface and a cost-free plan:

```sh
uv run python -m tests.semantic.evaluate --help
uv run python -m tests.semantic.evaluate --all-prompts
```

Only pass `--run` to the direct runner when paid execution is intended.

## Test strategy

The corpus is deliberately split into free deterministic tests and budgeted live-model
evaluations. A larger request count does not automatically make an evaluation more useful;
the goal is enough evidence for the decision being made.

### Ordinary development: free

Run the normal test suite. It verifies case construction, prompt definitions, expected
results, binary assets, evaluator behavior, and the rest of nfind without contacting a model:

```sh
make test
```

Changes unrelated to generation semantics should not trigger paid evaluation.

### Release smoke test: 20 requests

Run each canonical prompt once against the release's default model:

```sh
make semantic-eval-run EVAL_ARGS="--output eval-release.json"
```

This is a regression smoke test, not a statistically stable accuracy measurement. Report it
as 20 deterministic scenarios evaluated once, including the model identifier and date.

### Prompt or model changes: targeted first

Start with cases affected by the change, using repeated `--case` options. Expand to the 20
canonical prompts only when the targeted results justify it. Use all 60 variants for an
important model comparison or broad system-prompt change, not for routine commits.

### Instability calibration: approximately 15 requests

Occasionally repeat the most shortcut-prone canonical cases three times. The initial panel is:

- `markdown-files-with-install-heading`
- `xml-items-with-disabled-attribute`
- `javascript-files-with-default-export`
- `nonempty-directories-containing-only-audio-files`
- `mp3-files-by-id3-album`

These cases exercise fenced code, malformed structured data, comments versus syntax,
relational directory semantics, and real binary metadata. Repetition measures whether a
model's answer is stable; it should not be inferred from a single run. The evaluator currently
runs one sample per invocation, so perform three targeted invocations and retain all reports.

### Full matrix: exceptional, 60 requests per sample

Reserve `semantic-eval-run-all` for release candidates with major generation changes,
important model comparisons, or investigation of prompt sensitivity. Repeating the entire
matrix three times would cost 180 requests per model and is not the routine project standard.

### Reuse generated programs

Live reports retain the generated code, runtime, dependencies, and retry count. Use that code
for free deterministic replay and metamorphic fixture checks whenever the question is program
behavior rather than model-generation variability. Only generation requires another paid
request.

### Claims supported by this strategy

The suite supports claims about deterministic scenario coverage, release smoke results, and
directional model comparisons. A single run does not support a stable general accuracy rate or
a claim that one model is statistically superior. Use wording such as:

> nfind has a deterministic semantic evaluation suite covering 20 diverse filesystem
> scenarios and 60 human-reviewed prompt variants. Live-model results are budgeted regression
> measurements rather than statistically conclusive accuracy estimates.

## Fixture policy

Cases, prompts, and expected answers are committed and human-reviewed. Test-time fixture
materialization uses ordinary deterministic code; no model creates fixtures or ground truth.
Small checked-in binary assets are used when their real structure is part of the behavior.
