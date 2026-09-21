from __future__ import annotations

import threading
import time

from app.tools.card import make_card, resolve_concurrency
from app.tools.tool_search import TOOL_CALL_NAME, ToolSearchConfig, run_tool_calls


def _card(name: str, concurrency: str = "barrier"):
    return make_card(
        package="demo",
        name=name,
        handler=lambda **_: "ok",
        summary=name,
        description=name,
        display_name=name,
        display_icon="x",
        concurrency=concurrency,  # type: ignore[arg-type]
    )


def test_read_is_safe_write_is_barrier():
    assert resolve_concurrency(_card("read_file", "safe"), {}) == "safe"
    assert resolve_concurrency(_card("write_file", "barrier"), {}) == "barrier"


def test_process_list_safe_kill_barrier():
    card = _card("process", "barrier")
    assert resolve_concurrency(card, {"action": "list"}) == "safe"
    assert resolve_concurrency(card, {"action": "kill"}) == "barrier"
    assert resolve_concurrency(card, {"action": "wait"}) == "barrier"


def test_safe_batch_finishes_before_following_barrier(monkeypatch):
    events: list[str] = []

    def handler(name: str, delay: float = 0.0):
        def _run(**_kwargs):
            events.append(f"{name}:start")
            time.sleep(delay)
            events.append(f"{name}:end")
            return name

        return _run

    cards = [
        make_card(
            package="demo",
            name="safe_a",
            handler=handler("safe_a", 0.03),
            summary="safe_a",
            description="safe_a",
            display_name="safe_a",
            display_icon="x",
            concurrency="safe",
            source="plugin",
        ),
        make_card(
            package="demo",
            name="safe_b",
            handler=handler("safe_b", 0.03),
            summary="safe_b",
            description="safe_b",
            display_name="safe_b",
            display_icon="x",
            concurrency="safe",
            source="plugin",
        ),
        make_card(
            package="demo",
            name="barrier",
            handler=handler("barrier"),
            summary="barrier",
            description="barrier",
            display_name="barrier",
            display_icon="x",
            concurrency="barrier",
            source="plugin",
        ),
    ]
    monkeypatch.setattr(
        "app.tools.tool_search._cards_for_dispatch",
        lambda: (cards, ToolSearchConfig(enabled=False)),
    )

    results = run_tool_calls(
        [
            {"name": "safe_a", "id": "1", "args": {}},
            {"name": "safe_b", "id": "2", "args": {}},
            {"name": "barrier", "id": "3", "args": {}},
        ],
        "sess-1",
        process_for_cache=lambda msg, _session_id: msg,
    )

    assert [msg.tool_call_id for msg in results] == ["1", "2", "3"]
    assert events.index("barrier:start") > events.index("safe_a:end")
    assert events.index("barrier:start") > events.index("safe_b:end")


def test_safe_batch_isolates_one_member_failure(monkeypatch):
    def fail(**_kwargs):
        raise RuntimeError("boom")

    cards = [
        make_card(
            package="demo",
            name="safe_ok",
            handler=lambda **_kwargs: "ok",
            summary="safe_ok",
            description="safe_ok",
            display_name="safe_ok",
            display_icon="x",
            concurrency="safe",
            source="plugin",
        ),
        make_card(
            package="demo",
            name="safe_fail",
            handler=fail,
            summary="safe_fail",
            description="safe_fail",
            display_name="safe_fail",
            display_icon="x",
            concurrency="safe",
            source="plugin",
        ),
    ]
    monkeypatch.setattr(
        "app.tools.tool_search._cards_for_dispatch",
        lambda: (cards, ToolSearchConfig(enabled=False)),
    )

    results = run_tool_calls(
        [
            {"name": "safe_ok", "id": "ok", "args": {}},
            {"name": "safe_fail", "id": "fail", "args": {}},
        ],
        "sess-1",
        process_for_cache=lambda msg, _session_id: msg,
    )

    assert [msg.tool_call_id for msg in results] == ["ok", "fail"]
    assert results[0].content == "ok"
    assert results[1].additional_kwargs["reason_code"] == "tool_error"


def test_safe_batch_returns_completed_and_timeout_results(monkeypatch):
    def slow(**_kwargs):
        time.sleep(0.2)
        return "slow"

    cards = [
        make_card(
            package="demo",
            name="fast",
            handler=lambda **_kwargs: "fast",
            summary="fast",
            description="fast",
            display_name="fast",
            display_icon="x",
            concurrency="safe",
            source="plugin",
        ),
        make_card(
            package="demo",
            name="slow",
            handler=slow,
            summary="slow",
            description="slow",
            display_name="slow",
            display_icon="x",
            concurrency="safe",
            source="plugin",
        ),
    ]
    monkeypatch.setattr(
        "app.tools.tool_search._cards_for_dispatch",
        lambda: (cards, ToolSearchConfig(enabled=False)),
    )

    results = run_tool_calls(
        [
            {"name": "fast", "id": "fast-id", "args": {}},
            {"name": "slow", "id": "slow-id", "args": {}},
        ],
        "sess-1",
        process_for_cache=lambda msg, _session_id: msg,
        timeout_seconds=0.03,
    )

    assert [msg.tool_call_id for msg in results] == ["fast-id", "slow-id"]
    assert results[0].content == "fast"
    assert results[1].additional_kwargs["reason_code"] == "batch_timeout"


def test_safe_batch_preserves_project_workspace_context(monkeypatch, tmp_path):
    """并行 safe 工具必须能读到会话绑定的项目工作区（ContextVar / session 回绑）。"""
    from pathlib import Path

    from app.core.settings import Settings, init_workspace
    from app.storage.workspace import SessionStore
    from app.tools.runtime import get_project_workspace, set_tool_project

    home = tmp_path / "agent_home"
    defaults = Path(__file__).resolve().parents[1] / "workspace_defaults"
    config_defaults = Path(__file__).resolve().parents[1] / "app" / "config" / "defaults"
    if not config_defaults.exists():
        config_defaults = defaults
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    (project / "marker.txt").write_text("bound", encoding="utf-8")

    settings = Settings(
        workspace_path=home,
        workspace_defaults_path=defaults,
        config_dir=tmp_path / "config",
        config_defaults_path=config_defaults,
    )
    monkeypatch.setattr("app.core.settings.get_settings", lambda: settings)
    monkeypatch.setattr("app.storage.project.get_settings", lambda: settings)
    init_workspace(settings)
    store = SessionStore(home)
    sid = store.create(str(project))

    seen: list[str | None] = []

    def handler(**_kwargs):
        root = get_project_workspace()
        seen.append(str(root) if root else None)
        if root is None:
            raise RuntimeError("missing_workspace")
        return Path(root, "marker.txt").read_text(encoding="utf-8")

    cards = [
        make_card(
            package="demo",
            name="safe_a",
            handler=handler,
            summary="safe_a",
            description="safe_a",
            display_name="safe_a",
            display_icon="x",
            concurrency="safe",
            source="plugin",
            sandbox_policy="none",
        ),
        make_card(
            package="demo",
            name="safe_b",
            handler=handler,
            summary="safe_b",
            description="safe_b",
            display_name="safe_b",
            display_icon="x",
            concurrency="safe",
            source="plugin",
            sandbox_policy="none",
        ),
    ]
    monkeypatch.setattr(
        "app.tools.tool_search._cards_for_dispatch",
        lambda: (cards, ToolSearchConfig(enabled=False)),
    )
    # 父线程故意清空，模拟 ThreadPool 丢失 ContextVar；依赖 worker 内按 session 回绑
    set_tool_project(None)

    results = run_tool_calls(
        [
            {"name": "safe_a", "id": "1", "args": {}},
            {"name": "safe_b", "id": "2", "args": {}},
        ],
        sid,
        process_for_cache=lambda msg, _session_id: msg,
    )

    assert [msg.tool_call_id for msg in results] == ["1", "2"]
    assert all(msg.content == "bound" for msg in results)
    assert all(path == str(project.resolve()) for path in seen)


def test_cancelled_batch_marks_all_unstarted_calls(monkeypatch):
    cards = [
        _card("safe_a", "safe"),
        _card("safe_b", "safe"),
    ]
    cancel_event = threading.Event()
    cancel_event.set()
    monkeypatch.setattr(
        "app.tools.tool_search._cards_for_dispatch",
        lambda: (cards, ToolSearchConfig(enabled=False)),
    )

    results = run_tool_calls(
        [
            {"name": "safe_a", "id": "a", "args": {}},
            {"name": "safe_b", "id": "b", "args": {}},
        ],
        "sess-1",
        process_for_cache=lambda msg, _session_id: msg,
        cancel_event=cancel_event,
    )

    assert [msg.tool_call_id for msg in results] == ["a", "b"]
    assert all(msg.additional_kwargs["reason_code"] == "cancelled" for msg in results)


def test_mcp_status_and_deferred_tool_call_preserve_underlying_concurrency(monkeypatch):
    cards = [
        make_card(
            package="mcp",
            name="mcp_status",
            handler=lambda **_kwargs: "status",
            summary="mcp_status",
            description="mcp_status",
            display_name="mcp_status",
            display_icon="x",
            concurrency="safe",
            source="plugin",
        ),
        _card("list_directory", "safe"),
        _card("skill_list", "safe"),
    ]
    config = ToolSearchConfig(enabled=True, hot_tools=("list_directory",))
    monkeypatch.setattr(
        "app.tools.tool_search._cards_for_dispatch",
        lambda: (cards, config),
    )

    direct = run_tool_calls(
        [
            {"name": "mcp_status", "id": "status", "args": {}},
            {"name": "list_directory", "id": "list", "args": {}},
            {"name": "skill_list", "id": "skills", "args": {}},
        ],
        "sess-1",
        process_for_cache=lambda msg, _session_id: msg,
    )
    bridged = run_tool_calls(
        [
            {
                "name": TOOL_CALL_NAME,
                "id": "status-bridge",
                "args": {"name": "mcp_status", "arguments": {}},
            },
            {"name": "list_directory", "id": "list-bridge", "args": {}},
        ],
        "sess-1",
        process_for_cache=lambda msg, _session_id: msg,
    )

    assert [msg.tool_call_id for msg in direct] == ["status", "list", "skills"]
    assert [msg.tool_call_id for msg in bridged] == ["status-bridge", "list-bridge"]
    assert [msg.name for msg in bridged] == ["mcp_status", "list_directory"]
