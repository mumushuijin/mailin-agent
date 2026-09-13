from pathlib import Path

import pytest

from app.tools.packages.filesystem.handlers import glob_search, list_directory
from app.tools.runtime import set_tool_project


@pytest.fixture(autouse=True)
def _bind_project(tmp_path: Path, monkeypatch):
    home = tmp_path / "agent_home"
    defaults = tmp_path / "defaults"
    project = tmp_path / "project"
    home.mkdir()
    defaults.mkdir()
    project.mkdir()
    settings = type("S", (), {"workspace_path": home, "workspace_defaults_path": defaults})()
    monkeypatch.setattr("app.core.settings.get_settings", lambda: settings)
    set_tool_project(project)
    return project


def test_list_directory_empty_path_defaults_to_root():
    result = list_directory("")
    assert "路径不能为空" not in result


def test_glob_search_empty_directory_defaults_to_root():
    result = glob_search("*.md", "")
    assert "路径不能为空" not in result
