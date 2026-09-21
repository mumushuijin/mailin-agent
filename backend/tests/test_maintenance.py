from app.maintenance.tasks.mcp_health import check_mcp_health
from app.maintenance.tasks.memory_sediment import run_memory_sediment
from app.maintenance.types import MaintenanceResult


def test_sediment_skipped_when_nothing_pending(monkeypatch):
    monkeypatch.setattr(
        "app.maintenance.tasks.memory_sediment.maybe_auto_consolidate",
        lambda workspace=None: None,
    )
    result = run_memory_sediment()
    assert result.status == "skipped"
    assert not result.should_notify


def test_sediment_done_on_success(monkeypatch):
    monkeypatch.setattr(
        "app.maintenance.tasks.memory_sediment.maybe_auto_consolidate",
        lambda workspace=None: "已沉淀 3 条记忆",
    )
    result = run_memory_sediment()
    assert result.status == "done"
    assert result.success
    assert result.should_notify


def test_mcp_health_no_servers(monkeypatch):
    import app.maintenance.tasks.mcp_health as mcp_mod

    mcp_mod._last_health_snapshot = None
    calls = {"ensure": 0}
    monkeypatch.setattr(
        "app.maintenance.tasks.mcp_health.ensure_mcp_connected",
        lambda **_kwargs: calls.__setitem__("ensure", calls["ensure"] + 1) or False,
    )
    monkeypatch.setattr(
        "app.maintenance.tasks.mcp_health.build_mcp_status_payload",
        lambda: {"configured_servers": [], "servers": []},
    )
    result = check_mcp_health()
    assert result is None
    assert calls["ensure"] == 1


def test_mcp_health_disconnected(monkeypatch):
    import app.maintenance.tasks.mcp_health as mcp_mod

    mcp_mod._last_health_snapshot = None
    monkeypatch.setattr(
        "app.maintenance.tasks.mcp_health.build_mcp_status_payload",
        lambda: {
            "configured_servers": ["amap"],
            "servers": [{"name": "amap", "connected": False, "error": "timeout"}],
        },
    )
    result = check_mcp_health()
    assert result is not None
    assert result.success is False
    assert "未连接" in result.message


def test_sediment_would_run_respects_interval(monkeypatch, tmp_path):
    from app.maintenance.tasks.memory_sediment import sediment_would_run

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "2026-07-05.md").write_text("some notes", encoding="utf-8")
    state = {
        "files": {},
        "last_longterm_consolidate_at": "2026-07-05T08:00:00",
        "consolidate_count": 1,
    }
    (memory_dir / ".sediment_state.json").write_text(
        __import__("json").dumps(state), encoding="utf-8"
    )
    monkeypatch.setattr(
        "app.maintenance.tasks.memory_sediment.get_settings",
        lambda: type("S", (), {"workspace_path": tmp_path})(),
    )
    monkeypatch.setattr(
        "app.maintenance.tasks.memory_sediment._memory_config",
        lambda _ws: {"longterm_consolidate_days": 7, "consolidate_interval_hours": 24},
    )
    assert sediment_would_run(tmp_path) is False


def test_effective_mcp_timeout_caps_configured():
    from app.resilience.policies import MCP_DEFAULT_TIMEOUT_SECONDS, MCP_MAX_TIMEOUT_SECONDS, effective_mcp_timeout

    assert effective_mcp_timeout(None) == MCP_DEFAULT_TIMEOUT_SECONDS
    assert effective_mcp_timeout(300.0) == MCP_MAX_TIMEOUT_SECONDS
    assert effective_mcp_timeout(10.0) == 10.0


def test_mcp_policy_does_not_retry_auth_errors():
    from app.resilience.policies import mcp_call_policy

    policy = mcp_call_policy("test-server", timeout=25.0)
    assert policy.max_retries == 0
    assert policy.is_retryable(RuntimeError("401 Unauthorized")) is False
    assert policy.is_retryable(RuntimeError("peer closed connection")) is True


def test_mcp_invoke_card_skips_outer_execute_sync(monkeypatch):
    from app.tools.card import make_card
    from app.tools.tool_search import invoke_card

    calls = {"outer": 0, "inner": 0}

    def handler(**_kwargs):
        calls["inner"] += 1
        return '{"result":"ok"}'

    real_execute = __import__("app.tools.tool_search", fromlist=["execute_sync"]).execute_sync

    def counting_execute(dependency_id, fn, **kwargs):
        calls["outer"] += 1
        return real_execute(dependency_id, fn, **kwargs)

    monkeypatch.setattr("app.tools.tool_search.execute_sync", counting_execute)

    card = make_card(
        package="mcp-Bazi-MCP",
        name="mcp_Bazi_MCP_getChineseCalendar",
        handler=handler,
        summary="test",
        description="test",
        display_name="test",
        display_icon="🔌",
        source="plugin",
    )
    result = invoke_card(card, {})
    assert '"ok"' in result
    assert calls["inner"] == 1
    assert calls["outer"] == 0


def test_agent_tools_batch_timeout_scales_with_count():
    from app.resilience.policies import agent_tools_batch_timeout

    one = agent_tools_batch_timeout(1)
    three = agent_tools_batch_timeout(3)
    assert three > one
    assert agent_tools_batch_timeout(99) <= 120.0


def test_maintenance_result_notify_rules():
    skipped = MaintenanceResult(kind="sediment", message="skip", status="skipped")
    assert not skipped.should_notify

    mcp_ok = MaintenanceResult(
        kind="mcp_health",
        message="ok",
        success=True,
        detail={"state_changed": False},
    )
    assert not mcp_ok.should_notify

    mcp_changed = MaintenanceResult(
        kind="mcp_health",
        message="ok",
        success=True,
        detail={"state_changed": True},
    )
    assert mcp_changed.should_notify
