"""Cost-free tests for the opt-in live semantic evaluator."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nfind.runtimes import GeneratedFilter

from .cases import EXAMPLE_CASES, PYTHON_IMPORT_CASE, materialize_case
from .evaluate import (
    build_report,
    evaluate_job,
    evaluation_plan,
    main,
    selected_cases,
)


def test_evaluation_plan_defaults_to_twenty_canonical_prompts() -> None:
    jobs = evaluation_plan(EXAMPLE_CASES, all_prompts=False)

    assert len(jobs) == 20
    assert all(prompt_index == 0 for _case, prompt_index, _prompt in jobs)


def test_all_prompt_plan_contains_sixty_jobs() -> None:
    jobs = evaluation_plan(EXAMPLE_CASES, all_prompts=True)

    assert len(jobs) == 60
    assert {prompt_index for _case, prompt_index, _prompt in jobs} == {0, 1, 2}


def test_selected_cases_preserves_corpus_order_and_rejects_unknown() -> None:
    selected = selected_cases([EXAMPLE_CASES[3].id, EXAMPLE_CASES[0].id])

    assert selected == (EXAMPLE_CASES[0], EXAMPLE_CASES[3])
    with pytest.raises(ValueError, match="unknown case id"):
        selected_cases(["not-a-case"])


def test_evaluate_job_normalizes_results_and_scores_exactly(tmp_path: Path) -> None:
    materialize_case(PYTHON_IMPORT_CASE, tmp_path)

    def fake_search(root: Path, prompt: str, **kwargs: Any) -> list[dict[str, Any]]:
        assert prompt == PYTHON_IMPORT_CASE.query
        assert kwargs["model"] == "test/model"
        kwargs["on_retry"](1, ValueError("first reply was invalid"))
        kwargs["on_generated"](
            GeneratedFilter(
                code="def filter_paths(paths):\n    return paths\n",
                dependencies=["example-package"],
                runtime="python",
            )
        )
        return [{"path": str(root / "src/client.py")}]

    result = evaluate_job(
        PYTHON_IMPORT_CASE,
        0,
        PYTHON_IMPORT_CASE.query,
        tmp_path,
        model="test/model",
        sandbox_backend="fake",
        search=fake_search,
    )

    assert result.passed
    assert result.actual == ["src/client.py"]
    assert result.error is None
    assert result.generated_runtime == "python"
    assert result.generated_dependencies == ("example-package",)
    assert result.generated_code == "def filter_paths(paths):\n    return paths\n"
    assert result.generation_retries == 1


def test_evaluate_job_records_provider_or_sandbox_errors(tmp_path: Path) -> None:
    materialize_case(PYTHON_IMPORT_CASE, tmp_path)

    def failing_search(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("offline")

    result = evaluate_job(
        PYTHON_IMPORT_CASE,
        0,
        PYTHON_IMPORT_CASE.query,
        tmp_path,
        model="test/model",
        sandbox_backend="fake",
        search=failing_search,
    )
    report = build_report([result], model="test/model", sandbox_backend="fake")

    assert not result.passed
    assert result.error == "RuntimeError: offline"
    assert report["score"] == 0.0
    assert report["failed"] == 1


def test_main_only_prints_plan_without_explicit_run(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--all-prompts"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "60 paid model request(s)" in captured.err
    assert "No requests made" in captured.err
    assert captured.out == ""
