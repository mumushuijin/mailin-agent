"""每轮用户消息的迭代预算。

`max_steps` 统计的是 agent 节点调用次数（每步可并行多个 tool_call），
不是单个工具调用次数。到达上限后会解绑工具并强制总结，防止无限循环。
"""

from __future__ import annotations

from app.agent.messages import is_real_user_message
from app.agent.state import AgentState

# 默认预算：给 tool_search→describe→call、多文件编辑与测试修复留足空间
DEFAULT_MAX_STEPS = 48
# 硬上限：配置再大也会被钳制，防止失控循环耗尽资源
HARD_MAX_STEPS = 80
# 实用下限：至少允许一轮工具后再总结
MIN_MAX_STEPS = 4


def clamp_max_steps(raw: object | None, *, default: int = DEFAULT_MAX_STEPS) -> int:
    """将配置值钳制到 [MIN_MAX_STEPS, HARD_MAX_STEPS]。"""
    try:
        value = int(raw) if raw is not None else int(default)
    except (TypeError, ValueError):
        value = int(default)
    if value < MIN_MAX_STEPS:
        return MIN_MAX_STEPS
    if value > HARD_MAX_STEPS:
        return HARD_MAX_STEPS
    return value


class IterationBudget:
    """线程内使用的迭代计数器，映射到 state.step / state.max_steps。"""

    def __init__(self, used: int, max_total: int):
        self.used = max(0, used)
        self.max_total = clamp_max_steps(max_total)

    @classmethod
    def from_state(cls, state: AgentState) -> IterationBudget:
        messages = state.get("messages") or []
        if messages and is_real_user_message(messages[-1]):
            used = 0
        else:
            used = int(state.get("step") or 0)
        max_total = clamp_max_steps(state.get("max_steps"), default=DEFAULT_MAX_STEPS)
        return cls(used=used, max_total=max_total)

    @property
    def remaining(self) -> int:
        return max(0, self.max_total - self.used)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.max_total

    def next_used(self) -> int:
        return self.used + 1
