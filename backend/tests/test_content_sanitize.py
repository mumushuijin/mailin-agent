from __future__ import annotations

from langchain_core.messages import AIMessage

from app.agent.content_sanitize import (
    normalize_ai_message,
    parse_dsml_tool_calls,
    strip_dsml_markup,
)

_DSML_SAMPLE = """好的，信息齐了！

<｜｜DSML｜｜tool_calls>
<｜｜DSML｜｜invoke name="tool_call">
<｜｜DSML｜｜parameter name="name" string="true">mcp_Bazi_MCP_getBaziDetail</｜｜DSML｜｜parameter>
<｜｜DSML｜｜parameter name="arguments" string="true">{"solarDatetime": "2002-03-08T17:00:00+08:00", "gender": 1, "eightCharProviderSect": 2}</｜｜DSML｜｜parameter>
</｜｜DSML｜｜invoke>
</｜｜DSML｜｜tool_calls>"""


def test_parse_dsml_tool_call_proxy():
    calls = parse_dsml_tool_calls(_DSML_SAMPLE)
    assert len(calls) == 1
    assert calls[0]["name"] == "tool_call"
    assert calls[0]["args"]["name"] == "mcp_Bazi_MCP_getBaziDetail"
    assert calls[0]["args"]["arguments"]["gender"] == 1
    assert calls[0]["args"]["arguments"]["solarDatetime"] == "2002-03-08T17:00:00+08:00"


def test_strip_dsml_markup():
    cleaned = strip_dsml_markup(_DSML_SAMPLE)
    assert "DSML" not in cleaned
    assert "好的，信息齐了" in cleaned


def test_normalize_ai_message_recovers_tool_calls():
    msg = AIMessage(content=_DSML_SAMPLE, tool_calls=[])
    fixed = normalize_ai_message(msg)
    assert fixed.tool_calls
    assert fixed.tool_calls[0]["name"] == "tool_call"
    assert "DSML" not in str(fixed.content)


def test_normalize_preserves_existing_tool_calls():
    existing = [{"name": "tool_describe", "args": {"name": "x"}, "id": "call_1"}]
    msg = AIMessage(content=_DSML_SAMPLE, tool_calls=existing)
    fixed = normalize_ai_message(msg)
    assert len(fixed.tool_calls) == 2
    assert fixed.tool_calls[0]["name"] == "tool_describe"
