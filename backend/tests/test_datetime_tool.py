from __future__ import annotations

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.tools.packages.datetime.time import get_current_time, resolve_timezone


def test_resolve_timezone_from_iana_name(monkeypatch):
    monkeypatch.setattr(
        "app.tools.packages.datetime.time._read_user_md",
        lambda: "",
    )
    tz = resolve_timezone("Asia/Shanghai")
    if isinstance(tz, ZoneInfo):
        assert tz.key == "Asia/Shanghai"
    else:
        assert tz == dt_timezone(timedelta(hours=8))


def test_resolve_timezone_from_offset():
    tz = resolve_timezone("UTC+8")
    now = datetime.now(tz)
    offset = now.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 8 * 3600


def test_resolve_timezone_from_user_md(tmp_path: Path, monkeypatch):
    bootstraps = tmp_path / "bootstraps"
    bootstraps.mkdir()
    (bootstraps / "USER.md").write_text("时区：Asia/Tokyo\n", encoding="utf-8")

    monkeypatch.setattr(
        "app.tools.packages.datetime.time.get_settings",
        lambda: type("S", (), {"workspace_path": tmp_path})(),
    )

    tz = resolve_timezone()
    if isinstance(tz, ZoneInfo):
        assert tz.key == "Asia/Tokyo"
    else:
        assert tz == dt_timezone(timedelta(hours=9))


def test_resolve_timezone_invalid_explicit_falls_back_to_user_md(tmp_path: Path, monkeypatch):
    bootstraps = tmp_path / "bootstraps"
    bootstraps.mkdir()
    (bootstraps / "USER.md").write_text("时区：Asia/Tokyo\n", encoding="utf-8")

    monkeypatch.setattr(
        "app.tools.packages.datetime.time.get_settings",
        lambda: type("S", (), {"workspace_path": tmp_path})(),
    )

    tz = resolve_timezone("Not/A_Real_Zone")
    if isinstance(tz, ZoneInfo):
        assert tz.key == "Asia/Tokyo"
    else:
        assert tz == dt_timezone(timedelta(hours=9))


def test_get_current_time_output_structure():
    result = get_current_time("UTC")
    assert "当前时间:" in result
    assert "时区: UTC" in result
    assert "ISO:" in result
    assert "UTC:" in result


def test_registry_includes_datetime_when_enabled():
    from app.tools.registry import ToolRegistry

    registry = ToolRegistry(
        config={
            "tools": {
                "filesystem": False,
                "memory": False,
                "calculator": False,
                "web_search": False,
                "datetime": True,
            }
        }
    )
    names = {card.name for card in registry.resolve_cards()}
    assert names == {"get_current_time"}


def test_registry_excludes_datetime_when_disabled():
    from app.tools.registry import ToolRegistry

    registry = ToolRegistry(
        config={
            "tools": {
                "filesystem": False,
                "memory": False,
                "calculator": False,
                "web_search": False,
                "datetime": False,
            }
        }
    )
    names = {card.name for card in registry.resolve_cards()}
    assert "get_current_time" not in names
