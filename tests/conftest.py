"""Shared pytest fixtures.

We point MULTIAGENT_DATA_DIR at a tmp_path BEFORE importing store/history,
so DATA_DIR resolves to the test dir and we never touch the user's real
data files. Each test gets a fresh JsonStore cache and a fresh edits.jsonl.
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    """Reload store + history pointed at a clean tmp dir. Returns (store, history)."""
    monkeypatch.setenv("MULTIAGENT_DATA_DIR", str(tmp_path))

    # Make sure 'multiagent-tools' module dir is on sys.path so `import store` works
    # from the tests/ subdir.
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    # Drop any cached imports so the env var is picked up.
    for mod_name in ("store", "history"):
        if mod_name in sys.modules:
            del sys.modules[mod_name]

    store = importlib.import_module("store")
    history = importlib.import_module("history")
    return store, history
