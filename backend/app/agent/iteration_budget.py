"""每轮用户消息的迭代预算。"""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from app.agent.messages import is_real_user_message
from app.agent.state import AgentState

DEFAULT_MAX_STEPS = 24


class IterationBudget:
    """线程内使用的迭代计数器，映射到 state.step / state.max_steps。"""

    def __init__(self, used: int, max_total: int):
        self.used = max(0, used)
        self.max_total = max(1, max_total)

    @classmethod
    def from_state(cls, state: AgentState) -> IterationBudget:
        messages = state.get("messages") or []
        if messages and is_real_user_message(messages[-1]):
            used = 0
        else:
            used = int(state.get("step") or 0)
        max_total = int(state.get("max_steps") or DEFAULT_MAX_STEPS)
        return cls(used=used, max_total=max_total)

    @property
    def remaining(self) -> int:
        return max(0, self.max_total - self.used)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.max_total

    def next_used(self) -> int:
        return self.used + 1
