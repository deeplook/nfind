"""Shared pytest fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path_factory, monkeypatch):
    """Keep tests hermetic: never read a real user config or whitelist.

    Points XDG_CONFIG_HOME at a fresh temp dir and clears PFIND_CONFIG so the default
    config-file lookup finds nothing unless a test opts in. Tests that need a specific
    config/whitelist path still set it explicitly, overriding this.
    """
    monkeypatch.delenv("PFIND_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg")))
