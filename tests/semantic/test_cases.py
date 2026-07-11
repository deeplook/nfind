"""Contract tests for the first semantic-evaluation fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from .cases import (
    EXAMPLE_CASES,
    EXAMPLE_PROMPTS,
    MP3_ALBUM_CASE,
    PYTHON_IMPORT_CASE,
    SemanticCase,
    materialize_case,
)


@pytest.mark.parametrize("case", EXAMPLE_CASES, ids=lambda case: case.id)
def test_semantic_case_materializes_exact_declared_paths(
    tmp_path: Path, case: SemanticCase
) -> None:
    materialize_case(case, tmp_path)

    actual_files = {
        path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*") if path.is_file()
    }
    actual_nodes = {path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")}
    declared = {entry.path for entry in case.entries}

    assert actual_files == declared
    assert case.expected <= actual_nodes


def test_initial_semantic_corpus_has_twenty_distinct_cases() -> None:
    assert len(EXAMPLE_CASES) == 20
    assert len({case.id for case in EXAMPLE_CASES}) == 20


def test_each_case_has_three_distinct_nonempty_prompts() -> None:
    assert len(EXAMPLE_PROMPTS) == 60
    for case in EXAMPLE_CASES:
        assert len(case.prompts) == 3
        assert len(set(case.prompts)) == 3
        assert all(prompt.strip() == prompt for prompt in case.prompts)
        assert all(prompt for prompt in case.prompts)


def test_python_import_case_has_unambiguous_ground_truth(tmp_path: Path) -> None:
    materialize_case(PYTHON_IMPORT_CASE, tmp_path)

    matches = {
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*.py")
        if "import requests" in path.read_text().splitlines()
    }

    assert matches == PYTHON_IMPORT_CASE.expected


def test_mp3_case_contains_real_audio_with_distinct_id3_ground_truth(tmp_path: Path) -> None:
    materialize_case(MP3_ALBUM_CASE, tmp_path)

    matching = (tmp_path / "music/match.mp3").read_bytes()
    other = (tmp_path / "music/other.mp3").read_bytes()

    # Both assets have an ID3v2 header and MPEG audio frames; their committed tags
    # make the expected result reviewable without needing mutagen in the unit suite.
    assert matching.startswith(b"ID3")
    assert other.startswith(b"ID3")
    assert b"Night Signals" in matching
    assert b"Day Signals" in other
    assert b"Night Signals" not in other
    assert _contains_mpeg_frame(matching)
    assert _contains_mpeg_frame(other)
    assert MP3_ALBUM_CASE.expected == {"music/match.mp3"}


def _contains_mpeg_frame(data: bytes) -> bool:
    """Return whether bytes contain a plausible MPEG audio frame sync."""
    return any(
        data[index] == 0xFF and data[index + 1] & 0xE0 == 0xE0 for index in range(len(data) - 1)
    )
