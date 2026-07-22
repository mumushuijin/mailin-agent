from __future__ import annotations

from app.tools.packages.shell.truncate import truncate_tail


def test_truncate_noop_for_small_output():
    content = "line1\nline2\n"
    result = truncate_tail(content, max_lines=10, max_bytes=1024)
    assert result.truncated is False
    assert result.content == content


def test_truncate_by_lines():
    content = "\n".join(f"line{i}" for i in range(100))
    result = truncate_tail(content, max_lines=5, max_bytes=1024 * 1024)
    assert result.truncated is True
    assert result.truncated_by == "lines"
    assert "line99" in result.content
    assert "line0" not in result.content


def test_truncate_by_bytes():
    content = "x" * 200
    result = truncate_tail(content, max_lines=100, max_bytes=50)
    assert result.truncated is True
    assert result.truncated_by == "bytes"
    assert len(result.content.encode("utf-8")) <= 200
