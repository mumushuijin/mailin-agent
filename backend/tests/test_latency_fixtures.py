from __future__ import annotations

import json
from pathlib import Path

from tests.latency_fixtures import (
    build_dense_stream_events,
    build_long_history_messages,
    slow_tool,
    write_baseline_report,
)


def test_local_latency_fixtures_are_deterministic():
    history_a = build_long_history_messages(12)
    history_b = build_long_history_messages(12)
    dense_a = build_dense_stream_events(20)
    dense_b = build_dense_stream_events(20)

    assert [m.id for m in history_a] == [m.id for m in history_b]
    assert [e.data for e in dense_a] == [e.data for e in dense_b]
    assert slow_tool(0) == "ok"


def test_baseline_report_is_written_as_generated_artifact(tmp_path: Path):
    report_path = write_baseline_report(
        tmp_path / "latency-baseline.json",
        {
            "history_load_ms": [20, 25, 30, 40],
            "accepted_event_ms": [3, 4, 5, 6],
            "first_progress_ms": [10, 15, 20, 30],
            "cancellation_cleanup_ms": [12, 18, 20, 24],
            "tool_catalog_rebuild_count": 1,
        },
    )

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["history_load_ms"]["p50"] == 30.0
    assert payload["history_load_ms"]["p95"] == 40.0
    assert payload["tool_catalog_rebuild_count"] == 1
