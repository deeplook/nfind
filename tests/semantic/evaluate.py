"""Opt-in live-model evaluator for the deterministic semantic corpus.

Planning is free and is the default. Pass ``--run`` to make model requests:

    uv run python -m tests.semantic.evaluate --model openai/gpt-5.4 --run
    uv run python -m tests.semantic.evaluate --all-prompts --run
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from nfind import backend
from nfind.constants import DEFAULT_MODEL
from nfind.runtimes import GeneratedFilter

from .cases import EXAMPLE_CASES, SemanticCase, materialize_case

Search = Callable[..., list[dict[str, Any]]]


@dataclass(frozen=True)
class EvaluationResult:
    case_id: str
    prompt_index: int
    prompt: str
    expected: list[str]
    actual: list[str]
    passed: bool
    duration_seconds: float
    error: str | None = None
    generated_runtime: str | None = None
    generated_dependencies: tuple[str, ...] = ()
    generated_code: str | None = None
    generation_retries: int = 0


def selected_cases(case_ids: Sequence[str]) -> tuple[SemanticCase, ...]:
    """Return requested cases, preserving corpus order and rejecting unknown ids."""
    if not case_ids:
        return EXAMPLE_CASES
    requested = set(case_ids)
    known = {case.id for case in EXAMPLE_CASES}
    unknown = sorted(requested - known)
    if unknown:
        raise ValueError("unknown case id(s): " + ", ".join(unknown))
    return tuple(case for case in EXAMPLE_CASES if case.id in requested)


def evaluation_plan(
    cases: Sequence[SemanticCase], *, all_prompts: bool
) -> tuple[tuple[SemanticCase, int, str], ...]:
    """Expand cases into canonical-only or three-variant evaluation jobs."""
    return tuple(
        (case, prompt_index, prompt)
        for case in cases
        for prompt_index, prompt in enumerate(case.prompts if all_prompts else case.prompts[:1])
    )


def evaluate_job(
    case: SemanticCase,
    prompt_index: int,
    prompt: str,
    root: Path,
    *,
    model: str,
    sandbox_backend: str,
    search: Search = backend.search,
) -> EvaluationResult:
    """Generate and execute one filter, then compare normalized paths exactly."""
    started = time.monotonic()
    expected = sorted(case.expected)
    generated_filter: GeneratedFilter | None = None
    generation_retries = 0

    def on_generated(generated: GeneratedFilter) -> None:
        nonlocal generated_filter
        generated_filter = generated

    def on_retry(_retry: int, _error: ValueError) -> None:
        nonlocal generation_retries
        generation_retries += 1

    try:
        records = search(
            root,
            prompt,
            model=model,
            sandbox_backend=sandbox_backend,
            on_generated=on_generated,
            on_retry=on_retry,
        )
        actual = sorted(
            Path(record["path"]).resolve().relative_to(root.resolve()).as_posix()
            for record in records
        )
        return EvaluationResult(
            case_id=case.id,
            prompt_index=prompt_index,
            prompt=prompt,
            expected=expected,
            actual=actual,
            passed=actual == expected,
            duration_seconds=round(time.monotonic() - started, 3),
            generated_runtime=generated_filter.runtime if generated_filter else None,
            generated_dependencies=tuple(generated_filter.dependencies) if generated_filter else (),
            generated_code=generated_filter.code if generated_filter else None,
            generation_retries=generation_retries,
        )
    except Exception as exc:  # noqa: BLE001 - provider/sandbox failures belong in the report
        return EvaluationResult(
            case_id=case.id,
            prompt_index=prompt_index,
            prompt=prompt,
            expected=expected,
            actual=[],
            passed=False,
            duration_seconds=round(time.monotonic() - started, 3),
            error=f"{type(exc).__name__}: {exc}",
            generated_runtime=generated_filter.runtime if generated_filter else None,
            generated_dependencies=tuple(generated_filter.dependencies) if generated_filter else (),
            generated_code=generated_filter.code if generated_filter else None,
            generation_retries=generation_retries,
        )


def run_evaluation(
    jobs: Sequence[tuple[SemanticCase, int, str]],
    *,
    model: str,
    sandbox_backend: str,
    search: Search = backend.search,
) -> list[EvaluationResult]:
    """Run jobs sequentially, reusing one materialized tree per case."""
    results: list[EvaluationResult] = []
    with tempfile.TemporaryDirectory(prefix="nfind-eval-") as temporary:
        base = Path(temporary)
        roots: dict[str, Path] = {}
        for case, prompt_index, prompt in jobs:
            root = roots.get(case.id)
            if root is None:
                root = base / case.id
                root.mkdir()
                materialize_case(case, root)
                roots[case.id] = root
            result = evaluate_job(
                case,
                prompt_index,
                prompt,
                root,
                model=model,
                sandbox_backend=sandbox_backend,
                search=search,
            )
            results.append(result)
            status = "PASS" if result.passed else "FAIL"
            print(
                f"[{len(results)}/{len(jobs)}] {status} {case.id} #{prompt_index}", file=sys.stderr
            )
    return results


def build_report(
    results: Sequence[EvaluationResult], *, model: str, sandbox_backend: str
) -> dict[str, Any]:
    """Build a stable JSON-serializable score report."""
    passed = sum(result.passed for result in results)
    return {
        "model": model,
        "sandbox": sandbox_backend,
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "score": passed / len(results) if results else 0.0,
        "results": [asdict(result) for result in results],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--sandbox", default="docker")
    parser.add_argument("--case", action="append", default=[], help="Case id; repeatable")
    parser.add_argument(
        "--all-prompts",
        action="store_true",
        help="Evaluate all three prompt variants instead of canonical prompts only",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Make paid model requests; without this flag, only print the plan",
    )
    parser.add_argument("--output", type=Path, help="Write the JSON report to this file")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        cases = selected_cases(args.case)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    jobs = evaluation_plan(cases, all_prompts=args.all_prompts)
    if not args.run:
        scope = "all variants" if args.all_prompts else "canonical prompts"
        print(
            f"Plan: {len(jobs)} paid model request(s) using {args.model} ({scope}).\n"
            "No requests made. Pass --run to execute.",
            file=sys.stderr,
        )
        return 0

    results = run_evaluation(jobs, model=args.model, sandbox_backend=args.sandbox)
    report = build_report(results, model=args.model, sandbox_backend=args.sandbox)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(rendered, encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(rendered, end="")
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
