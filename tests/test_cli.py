"""Unit tests for the CLI that need no sandbox backend or LLM."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import nfind
from nfind import cli


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_flag_prints_version_and_exits(flag: str) -> None:
    result = CliRunner().invoke(cli.app, [flag])
    assert result.exit_code == 0
    assert result.output.strip() == f"nfind {nfind.__version__}"
