from __future__ import annotations

from app.tools.mcp import lifecycle as lc
from app.tools.mcp.types import McpServerConfig


def test_server_connect_gives_up_after_max_attempts():
    lc._reset_connect_failures()
    lc._server_errors.clear()
    name = "test-server"
    for i in range(1, lc._MAX_CONNECT_ATTEMPTS + 1):
        assert not lc.server_connect_gave_up(name)
        lc._record_connect_failure(name, f"fail {i}")
    assert lc.server_connect_gave_up(name)
    assert "暂停自动连接" in (lc.get_server_connect_error(name) or "")


def test_reset_on_success():
    lc._record_connect_failure("srv", "err")
    lc._record_connect_success("srv")
    assert not lc.server_connect_gave_up("srv")
    assert lc.get_server_connect_error("srv") is None


def test_ensure_mcp_connected_non_blocking_when_gave_up(monkeypatch):
    lc._reset_connect_failures()
    lc._servers.clear()
    for _ in range(lc._MAX_CONNECT_ATTEMPTS):
        lc._record_connect_failure("bing", "timeout")

    called = {"discover": False}

    def _fake_discover():
        called["discover"] = True
        return []

    monkeypatch.setattr(lc, "load_mcp_server_configs", lambda: {"bing": object()})
    monkeypatch.setattr(lc, "discover_mcp_servers", _fake_discover)

    result = lc.ensure_mcp_connected(blocking=True)
    assert result is False
    assert called["discover"] is False


def test_mcp_status_cache_reuses_snapshot_and_invalidates_on_health_change(monkeypatch):
    lc._reset_connect_failures()
    lc._server_errors.clear()
    lc._servers.clear()
    lc.clear_mcp_status_cache()
    calls = {"ensure": 0}

    def fake_ensure(*, blocking=False):
        calls["ensure"] += 1
        return False

    monkeypatch.setattr(lc, "ensure_mcp_connected", fake_ensure)
    monkeypatch.setattr(lc, "load_mcp_server_configs", lambda: {"srv": McpServerConfig(name="srv")})
    monkeypatch.setattr(lc, "get_mcp_cards", lambda: [])

    first = lc.get_mcp_status()
    second = lc.get_mcp_status()
    assert calls["ensure"] == 1
    assert first[0].name == second[0].name == "srv"

    lc._record_connect_failure("srv", "boom")
    third = lc.get_mcp_status()
    assert calls["ensure"] == 2
    assert third[0].error == "boom"
