from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.logging import _add_timestamp, setup_logging


def test_log_timestamp_uses_shanghai_timezone(monkeypatch):
    monkeypatch.setenv("MAILIN_TIMEZONE", "Asia/Shanghai")
    from app.core.settings import get_settings

    get_settings.cache_clear()

    event = _add_timestamp(None, "info", {})
    ts = datetime.fromisoformat(event["timestamp"])
    assert ts.tzinfo is not None
    assert ts.utcoffset() == ZoneInfo("Asia/Shanghai").utcoffset(ts)


def test_setup_logging_idempotent():
    setup_logging(level="INFO", log_format="console")
    setup_logging(level="DEBUG", log_format="json")
