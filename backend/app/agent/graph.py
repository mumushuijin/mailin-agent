from functools import lru_cache
import time

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from app.agent.nodes.agent import call_agent
from app.agent.nodes.router import should_continue
from app.agent.nodes.tools import call_tools
from app.agent.state import AgentState
from app.storage.checkpoint import get_checkpointer
from app.tools.registry import get_tools


def _resolve_checkpointer():
    try:
        return get_checkpointer()
    except RuntimeError:
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()


def build_graph():
    tools = get_tools()
    builder = StateGraph(AgentState)
    builder.add_node("agent", call_agent)
    builder.add_edge(START, "agent")
    if tools:
        builder.add_node("tools", call_tools)
        builder.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
        builder.add_edge("tools", "agent")
    else:
        builder.add_edge("agent", END)
    return builder.compile(checkpointer=_resolve_checkpointer())


@lru_cache
def get_graph():
    return build_graph()


def make_thread_config(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id}}


def make_initial_state(message: str, max_steps: int) -> dict:
    return {
        "messages": [
            HumanMessage(content=message, additional_kwargs={"timestamp": int(time.time())})
        ],
        "step": 0,
        "max_steps": max_steps,
        "context_summary": "",
        "compression_count": 0,
        "memory_turn_counter": 0,
        "memory_nudge_pending": False,
        "context_usage": {},
        "api_usage": {},
        "session_token_stats": {},
        "last_invoke_ledger_len": 0,
    }
