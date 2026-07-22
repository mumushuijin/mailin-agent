from langchain_core.messages import AIMessage

from langgraph.graph import END

from app.agent.iteration_budget import IterationBudget
from app.agent.state import AgentState


def effective_step(state: AgentState) -> int:
    """新用户轮开始时重置步数（末条消息为 HumanMessage）。"""
    return IterationBudget.from_state(state).used


def should_continue(state: AgentState) -> str:
    messages = state["messages"]
    if not messages:
        return END

    last = messages[-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"

    return END
