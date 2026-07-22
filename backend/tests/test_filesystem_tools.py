from pathlib import Path

import pytest

from app.tools.packages.filesystem import handlers as fs


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "app.tools.packages.filesystem.handlers.get_settings",
        lambda: type("S", (), {"workspace_path": tmp_path})(),
    )
    return tmp_path


def test_read_write_replace(workspace: Path):
    assert "已写入" in fs.write_file("notes.txt", "hello world")
    assert fs.read_file("notes.txt") == "hello world"
    assert "替换 1 处" in fs.replace_in_file("notes.txt", "world", "mailin")
    assert fs.read_file("notes.txt") == "hello mailin"


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
    assert "other.txt" not in result


def test_write_defaults_to_artifacts(workspace: Path):
    result = fs.write_file("demo.html", "<html></html>")
    assert "artifacts/demo.html" in result or "artifacts\\demo.html" in result
    assert fs.read_file("artifacts/demo.html") == "<html></html>"


def test_move_and_delete(workspace: Path):
    fs.write_file("a.txt", "data")
    assert "已移动" in fs.move_file("a.txt", "archive/a.txt")
    assert fs.read_file("archive/a.txt") == "data"
    assert "不存在" in fs.read_file("a.txt")

    assert "已删除" in fs.delete_file("archive/a.txt")
    assert "不存在" in fs.read_file("archive/a.txt")
