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

    last = messages[-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return last

    return None


def should_continue(state: AgentState) -> str:
    return "tools" if pending_tool_message(state) else END
