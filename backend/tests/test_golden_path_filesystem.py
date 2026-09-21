"""无 MCP 时的文件编辑 golden-path（读 → 替换 → undo）。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.tools.packages.filesystem import handlers as fs
from app.tools.runtime import set_tool_project, set_tool_session


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch):
    home = tmp_path / "agent_home"
    defaults = tmp_path / "defaults"
    project = tmp_path / "project"
    home.mkdir()
    defaults.mkdir()
    project.mkdir()
    (project / "app.py").write_text("print('hello')\n", encoding="utf-8")
    settings = type("S", (), {"workspace_path": home, "workspace_defaults_path": defaults})()
    monkeypatch.setattr("app.core.settings.get_settings", lambda: settings)
    monkeypatch.setattr("app.tools.packages.filesystem.handlers.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.tools.registry.load_full_config",
        lambda workspace=None: {
            "tools": {
                "enforcement_mode": "enforce",
                "filesystem": {"snapshots": {"enabled": True}},
            }
        },
    )
    set_tool_project(project)
    set_tool_session("golden")
    return project


def test_golden_path_edit_and_undo(workspace: Path):
    content = fs.read_file("app.py")
    assert "hello" in content

    edited = fs.replace_in_file("app.py", "hello", "mailin")
    m = re.search(r"\[snapshot_id=([0-9a-f]+)\]", edited)
    assert m
    assert "mailin" in fs.read_file("app.py")

    # 查看 diff 摘要：前后内容不同
    assert "hello" not in fs.read_file("app.py")

    undone = fs.undo_file_change(m.group(1))
    assert "撤销" in undone
    assert "hello" in fs.read_file("app.py")
