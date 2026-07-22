from __future__ import annotations


def build_tools_disclosure_context(config: dict | None = None) -> str:
    """生成注入 System 的工具披露说明，帮助模型走 tool_search 披露链。"""
    from app.tools.registry import ToolRegistry, load_full_config
    from app.tools.tool_search import (
        _summarize_deferred_packages,
        classify_cards,
        load_tool_search_config,
    )

    full = config if config is not None else load_full_config()
    ts_config = load_tool_search_config(full)
    if not ts_config.enabled:
        return ""

    registry = ToolRegistry(config=full)
    cards = registry.resolve_cards()
    hot, deferred = classify_cards(cards, ts_config)
    if not deferred:
        return ""

    hot_names = [c.name for c in hot]
    parts = [
        "## 工具能力说明",
        "",
        "部分能力未直接 bind，须按序调用：**tool_search** → **tool_describe** → **tool_call**。",
        "",
        f"**热工具（可直接调用）**：{', '.join(hot_names) if hot_names else '（无）'}",
        "",
        "**延时工具目录概览**（内置包与 MCP 各服务同级；MCP 具体工具与 read_file/write_file 同级）：",
        _summarize_deferred_packages(deferred),
        "",
        "需要搜索、文件写入、外部集成等能力时，**直接** tool_search 按关键词检索"
        "（如「bing 搜索」「写入文件」），再 tool_describe → tool_call。",
        "勿为普通任务先查 mcp_status。",
        "仅当用户明确询问 MCP 连接/可用性，或调用失败需排查时，再 tool_search「mcp 状态」并使用 mcp_status。",
    ]

    return "\n".join(parts).strip()
