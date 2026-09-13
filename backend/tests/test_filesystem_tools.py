from pathlib import Path

import pytest

from app.tools.packages.filesystem import handlers as fs
from app.tools.runtime import set_tool_project


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch):
    home = tmp_path / "agent_home"
    defaults = tmp_path / "defaults"
    project = tmp_path / "project"
    home.mkdir()
    defaults.mkdir()
    project.mkdir()
    settings = type("S", (), {"workspace_path": home, "workspace_defaults_path": defaults})()
    monkeypatch.setattr("app.core.settings.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.tools.packages.filesystem.handlers.get_settings",
        lambda: settings,
    )
    set_tool_project(project)
    return project


def test_read_write_replace(workspace: Path):
    assert "已写入" in fs.write_file("notes.txt", "hello world")
    assert "1|hello world" in fs.read_file("notes.txt")
    assert "替换 1 处" in fs.replace_in_file("notes.txt", "world", "mailin")
    assert "1|hello mailin" in fs.read_file("notes.txt")


def test_list_and_glob(workspace: Path):
    fs.mkdir("src/components")
    fs.write_file("src/main.py", "print('hi')")
    fs.write_file("src/components/App.vue", "<template/>")

    listing = fs.list_directory("src")
    assert "[dir] src\\components" in listing or "[dir] src/components" in listing
    assert "[file]" in listing

    matches = fs.glob_search("**/*.py", "src")
    assert "main.py" in matches


def test_search_files(workspace: Path):
    fs.write_file("docs/readme.md", "Mailin 是一个本地助手")
    fs.write_file("docs/other.txt", "无关内容")

    result = fs.search_files("Mailin", "docs", file_pattern="*.md")
    assert "readme.md" in result
    assert "Mailin" in result
    assert "L1:" in result
    assert "other.txt" not in result


def test_search_files_regex_and_invalid(workspace: Path):
    fs.write_file("a.py", "assert value == 1\n")
    hit = fs.search_files(r"assert .+ ==", ".")
    assert "a.py" in hit
    assert "L1:" in hit
    bad = fs.search_files(r"(", ".")
    assert "无效的正则表达式" in bad


def test_read_file_line_numbers_window(workspace: Path):
    fs.write_file("lines.txt", "a\nb\nc\n")
    window = fs.read_file("lines.txt", offset=2, limit=1)
    assert "2|b" in window
    assert "1|a" not in window


def test_write_defaults_to_artifacts(workspace: Path):
    result = fs.write_file("demo.html", "<html></html>")
    assert ".mailin/artifacts/demo.html" in result.replace("\\", "/")
    assert fs.read_file(".mailin/artifacts/demo.html") == "1|<html></html>"


def test_move_and_delete(workspace: Path):
    fs.write_file("a.txt", "data")
    assert "已移动" in fs.move_file("a.txt", "archive/a.txt")
    assert fs.read_file("archive/a.txt") == "1|data"
    assert "不存在" in fs.read_file("a.txt")

    assert "已删除" in fs.delete_file("archive/a.txt")
    assert "不存在" in fs.read_file("archive/a.txt")
