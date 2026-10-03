from __future__ import annotations

import os
import pytest

os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
os.environ.setdefault("LANGSMITH_TRACING", "false")


@pytest.fixture(autouse=True)
def isolated_runtime_root(monkeypatch, tmp_path):
    from app.core.settings import get_settings

    monkeypatch.setenv("MAILIN_RUNTIME_ROOT", str(tmp_path / "runtime"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
