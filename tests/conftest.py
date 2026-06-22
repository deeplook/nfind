"""Shared pytest fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path_factory, monkeypatch):
    """Keep tests hermetic: never read or write a real user config, whitelist, or cache.

    Points XDG_CONFIG_HOME/XDG_CACHE_HOME at fresh temp dirs and clears PFIND_CONFIG and
    PFIND_ENDPOINT_CACHE so the default config-file and endpoint-cache lookups find nothing
    unless a test opts in. Tests that need a specific path still set it explicitly,
    overriding this.
    """
    monkeypatch.delenv("PFIND_CONFIG", raising=False)
    monkeypatch.delenv("PFIND_ENDPOINT_CACHE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg")))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("cache")))
