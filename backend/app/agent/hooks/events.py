"""钩子事件名常量与合法事件集合。

事件语义映射本项目 LangGraph ReAct 执行流：
- 轮级（每个用户消息一次）：on_session_start / pre_llm_call / post_llm_call / on_session_end
- 步级（每次 agent 节点一次）：pre_agent_step
- 工具级（每个 tool_call 一次）：pre_tool_call / transform_tool_result / post_tool_call
"""

from __future__ import annotations

ON_SESSION_START = "on_session_start"
ON_SESSION_END = "on_session_end"
PRE_LLM_CALL = "pre_llm_call"
POST_LLM_CALL = "post_llm_call"
PRE_AGENT_STEP = "pre_agent_step"
PRE_TOOL_CALL = "pre_tool_call"
POST_TOOL_CALL = "post_tool_call"
TRANSFORM_TOOL_RESULT = "transform_tool_result"

VALID_HOOKS = frozenset(
    {
        ON_SESSION_START,
        ON_SESSION_END,
        PRE_LLM_CALL,
        POST_LLM_CALL,
        PRE_AGENT_STEP,
        PRE_TOOL_CALL,
        POST_TOOL_CALL,
        TRANSFORM_TOOL_RESULT,
    }
)

INTERVENTION_HOOKS = frozenset({PRE_TOOL_CALL, PRE_LLM_CALL, TRANSFORM_TOOL_RESULT})
