from pathlib import Path

import pytest

from app.core.runtime_layout import REPOSITORY_ROOT, RuntimeLayout


def test_development_layout_uses_one_repo_local_root(monkeypatch) -> None:
    monkeypatch.delenv("MAILIN_RUNTIME_ROOT")
    layout = RuntimeLayout.resolve()
    assert layout.runtime_root == REPOSITORY_ROOT / ".runtime"
    assert {path.relative_to(layout.runtime_root).parts[0] for path in (layout.data, layout.cache, layout.tmp, layout.log)} == {"data", "cache", "tmp", "log"}
    assert layout.agent_home == layout.data / "agent-home"
    assert layout.config_path == layout.data / "config" / "config.toml"


def test_layout_rejects_resource_overlap(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="不能重叠"):
        RuntimeLayout.resolve(tmp_path / "resources" / "data", tmp_path / "resources")


def test_layout_creates_only_four_managed_directories(tmp_path: Path) -> None:
    layout = RuntimeLayout.resolve(tmp_path / "runtime", tmp_path / "resources")
    layout.ensure_writable()
    assert {item.name for item in layout.runtime_root.iterdir()} == {"data", "cache", "tmp", "log"}
