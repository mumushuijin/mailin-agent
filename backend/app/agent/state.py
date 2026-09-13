from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    # 会话账本：完整记录，只追加，永不压缩
    messages: Annotated[list, add_messages]
    step: int
    max_steps: int
    # 上下文引擎状态（持久化到 checkpoint）
    context_summary: str
    compression_count: int
    memory_turn_counter: int
    memory_nudge_pending: bool
    context_usage: dict[str, Any]
    api_usage: dict[str, Any]
    session_token_stats: dict[str, Any]
    last_invoke_ledger_len: int
    # 运行时字段（每轮组装，不写 checkpoint 亦可）
    working_messages: list
    todos: list
