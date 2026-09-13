from __future__ import annotations

import json
import time
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.streaming.events import AgentEvent


def build_long_history_messages(count: int = 2_000):
    messages = []
    for i in range(count):
        if i % 3 == 0:
            messages.append(HumanMessage(content=f"user-{i}", id=f"h{i}"))
        elif i % 3 == 1:
            messages.append(AIMessage(content=f"assistant-{i}", id=f"a{i}"))
        else:
            messages.append(ToolMessage(content=f"tool-{i}", tool_call_id=f"call-{i}", id=f"t{i}", name="read_file"))
    return messages


def build_dense_stream_events(count: int = 1_000) -> list[AgentEvent]:
    return [
        AgentEvent("chunk", {"content": str(i % 10)}, run_id="fixture-run")
        for i in range(count)
    ]


async def slow_memory_prep(delay_seconds: float = 0.05) -> None:
    import asyncio

    await asyncio.sleep(delay_seconds)


def slow_tool(delay_seconds: float = 0.05) -> str:
    time.sleep(delay_seconds)
    return "ok"


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return float(ordered[index])


def write_baseline_report(path: Path, measurements: dict[str, list[float] | float | int]) -> Path:
    report: dict[str, object] = {}
    for name, values in measurements.items():
        if isinstance(values, list):
            report[name] = {
                "p50": percentile([float(v) for v in values], 0.50),
                "p95": percentile([float(v) for v in values], 0.95),
                "samples": len(values),
            }
        else:
            report[name] = values
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
