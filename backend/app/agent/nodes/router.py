from langchain_core.messages import AIMessage

from langgraph.graph import END

from app.agent.iteration_budget import IterationBudget
from app.agent.state import AgentState


def effective_step(state: AgentState) -> int:
    """新用户轮开始时重置步数（末条消息为 HumanMessage）。"""
    return IterationBudget.from_state(state).used


def pending_tool_message(state: AgentState) -> AIMessage | None:
    messages = (state.get("context") or {}).get("working_message") or []
    if not messages:
        return None

    from langchain_core.messages import ToolMessage
    settled = {message.tool_call_id for message in messages if isinstance(message, ToolMessage)}
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            if message.tool_calls and any(call["id"] not in settled for call in message.tool_calls):
                return message
            return None

    return None


def should_continue(state: AgentState) -> str:
    if state.get("turn_status") in {"completed", "failed", "cancelled", "handoff"}:
        return END
    return "tools" if pending_tool_message(state) else END
