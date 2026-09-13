import json
import logging
import re
import time
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.agent.messages import is_system_maintenance, make_system_message
from langchain_core.runnables import RunnableConfig

from app.agent.hooks import dispatch_pre_agent_step, dispatch_pre_llm_call
from app.agent.iteration_budget import IterationBudget
from app.agent.state import AgentState
from app.context.api_usage import (
    build_step_api_usage,
    extract_api_usage,
    merge_session_token_stats,
)
from app.agent.content_sanitize import normalize_ai_message
from app.context.engine import assemble_context
from app.context.tool_cache import defer_cache_tool_messages, merge_tool_cache_updates
from app.core.llm import get_chat_model
from app.resilience import (
    CallContext,
    AGENT_FAILURE_MESSAGE,
    execute_sync,
    llm_agent_policy,
    llm_grace_policy,
)
from app.tools.registry import get_tools
from app.storage.project import bind_session_runtime

logger = logging.getLogger(__name__)

_BUDGET_SUMMARY_PROMPT = (
    "你已到达本轮工具调用次数上限。请根据目前已掌握的信息，给出最终回答总结，不要再调用任何工具。"
)
_BUDGET_SUMMARY_FALLBACK = (
    "已达到本轮工具调用上限，但未能生成完整总结。请参考上文中的工具执行结果。"
)


def _is_context_length_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        kw in text
        for kw in ("context length", "maximum context", "413", "token limit", "too many tokens")
    )


def _normalize_search_query(query: str) -> str:
    return re.sub(r"\s+", " ", query.strip().lower())


def _recent_search_queries(messages: list[BaseMessage], limit: int = 8) -> set[str]:
    queries: set[str] = set()
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.get("name") != "web_search":
                    continue
                args = tc.get("args") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                query = args.get("query") if isinstance(args, dict) else None
                if isinstance(query, str) and query.strip():
                    queries.add(_normalize_search_query(query))
                    if len(queries) >= limit:
                        return queries
    return queries


def _strip_duplicate_tool_calls(response: AIMessage, messages: list[BaseMessage]) -> AIMessage:
    if not response.tool_calls:
        return response

    recent = _recent_search_queries(messages)
    kept = []
    blocked_search = False
    for tc in response.tool_calls:
        name = tc.get("name")
        args = tc.get("args") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if name == "web_search" and isinstance(args, dict):
            query = args.get("query")
            if isinstance(query, str) and _normalize_search_query(query) in recent:
                blocked_search = True
                continue
        kept.append(tc)

    if kept:
        return AIMessage(
            content=response.content,
            id=response.id,
            tool_calls=kept,
            usage_metadata=response.usage_metadata,
            response_metadata=dict(response.response_metadata or {}),
            additional_kwargs=dict(response.additional_kwargs or {}),
        )

    if blocked_search:
        return AIMessage(
            content=(
                (response.content or "")
                + "\n\n（已阻止重复的网络搜索：相同 query 在本轮对话中已有结果，请直接根据已有搜索结果回答用户。）"
            ).strip(),
            id=response.id,
            tool_calls=[],
            usage_metadata=response.usage_metadata,
            response_metadata=dict(response.response_metadata or {}),
            additional_kwargs=dict(response.additional_kwargs or {}),
        )
    return response


def _build_token_usage_updates(
    state: AgentState,
    assembled,
    response: BaseMessage | None,
) -> dict[str, Any]:
    """合并本步 API 真实用量与会话累计统计。"""
    agent_usage = extract_api_usage(response, source="agent") if response else None
    compression_usages = list(assembled.compression_api_usages or [])
    estimated_prompt = assembled.usage.total_tokens if assembled.usage else None

    api_usage = build_step_api_usage(
        agent_usage=agent_usage,
        compression_usages=compression_usages,
        estimated_prompt_tokens=estimated_prompt,
    )
    session_token_stats = merge_session_token_stats(
        state.get("session_token_stats"),
        api_usage,
    )
    updates: dict[str, Any] = {"session_token_stats": session_token_stats}
    if api_usage:
        updates["api_usage"] = api_usage
    return updates


def _ai_has_content(message: AIMessage) -> bool:
    content = message.content
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        text = "".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        )
        return bool(text.strip())
    return False


def _needs_grace_summary(response: AIMessage) -> bool:
    if response.tool_calls:
        return True
    return not _ai_has_content(response)


def _invoke_llm(
    model,
    messages: list,
    session_id: str | None,
    *,
    grace: bool = False,
    config: RunnableConfig | None = None,
):
    policy = llm_grace_policy() if grace else llm_agent_policy()
    # 显式透传 config：execute_sync 会在独立线程执行，依赖 contextvars 的流式回调
    # 无法跨线程自动传播，必须把 config（含 astream_events 回调）显式传给 invoke。
    outcome = execute_sync(
        policy.dependency_id,
        lambda: model.invoke(messages, config=config),
        policy=policy,
        context=CallContext(session_id=session_id),
    )
    if outcome.ok:
        return outcome.value, None
    return None, outcome.error or "LLM 调用失败"


def _run_grace_summary(
    model,
    working: list,
    session_id: str | None,
    config: RunnableConfig | None = None,
) -> tuple[HumanMessage, AIMessage]:
    summary_human = make_system_message(_BUDGET_SUMMARY_PROMPT, "budget_summary")
    grace_response, err = _invoke_llm(
        model,
        list(working) + [summary_human],
        session_id,
        grace=True,
        config=config,
    )
    if grace_response is None:
        logger.warning("grace summary 失败: %s", err)
        grace_response = AIMessage(content=_BUDGET_SUMMARY_FALLBACK)
    elif not isinstance(grace_response, AIMessage):
        grace_response = AIMessage(content=_BUDGET_SUMMARY_FALLBACK)
    else:
        grace_response = normalize_ai_message(grace_response)
        if not _ai_has_content(grace_response):
            grace_response = AIMessage(
                content=_BUDGET_SUMMARY_FALLBACK,
                id=grace_response.id,
                usage_metadata=grace_response.usage_metadata,
                response_metadata=dict(grace_response.response_metadata or {}),
                additional_kwargs=dict(grace_response.additional_kwargs or {}),
            )
    if "timestamp" not in grace_response.additional_kwargs:
        grace_response.additional_kwargs["timestamp"] = int(time.time())
    return summary_human, grace_response


def _merge_token_usage_updates(
    state: AgentState,
    assembled,
    *responses: BaseMessage | None,
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for response in responses:
        updates = _build_token_usage_updates(state, assembled, response)
        if updates.get("api_usage"):
            merged["api_usage"] = updates["api_usage"]
        if updates.get("session_token_stats"):
            state = {**state, "session_token_stats": updates["session_token_stats"]}
            merged["session_token_stats"] = updates["session_token_stats"]
    return merged


def _ledger_len_after_invoke(start_len: int, out_messages: list, cache_updates: list) -> int:
    """cache_updates 为同 id 覆盖，不计入账本长度增量。"""
    replacements = len(cache_updates)
    appended = max(0, len(out_messages) - replacements)
    return start_len + appended


def _agent_error_payload(
    state: AgentState,
    budget: IterationBudget,
    assembled,
    cache_updates: list,
    content: str,
    *,
    invoke_start_ledger_len: int,
    extra_kwargs: dict | None = None,
) -> dict[str, Any]:
    token_updates = _build_token_usage_updates(state, assembled, None)
    kwargs = {"timestamp": int(time.time())}
    if extra_kwargs:
        kwargs.update(extra_kwargs)
    messages: list = list(cache_updates)
    messages.append(AIMessage(content=content, additional_kwargs=kwargs))
    usage_dict = assembled.usage.to_dict() if assembled and assembled.usage else {}
    return {
        "messages": messages,
        "step": budget.next_used(),
        "context_summary": getattr(assembled, "context_summary", "") or "",
        "compression_count": getattr(assembled, "compression_count", 0) or 0,
        "memory_turn_counter": getattr(assembled, "memory_turn_counter", 0) or 0,
        "memory_nudge_pending": bool(getattr(assembled, "memory_nudge_pending", False)),
        "context_usage": usage_dict,
        "working_messages": getattr(assembled, "messages", []) or [],
        "last_invoke_ledger_len": _ledger_len_after_invoke(
            invoke_start_ledger_len, messages, cache_updates
        ),
        **token_updates,
    }


def call_agent(state: AgentState, config: RunnableConfig) -> dict:
    session_id = config.get("configurable", {}).get("thread_id")
    bind_session_runtime(session_id)
    model = get_chat_model()
    tools = get_tools()
    ledger = state.get("messages") or []
    cache_updates = defer_cache_tool_messages(ledger, session_id or "default")
    ledger_with_cache = merge_tool_cache_updates(ledger, cache_updates)
    state_for_context = {**state, "messages": ledger_with_cache} if cache_updates else state
    invoke_start_ledger_len = len(ledger_with_cache)
    budget = IterationBudget.from_state(state)
    dispatch_pre_agent_step(
        session_id=session_id,
        step=budget.used,
        max_steps=budget.max_total,
    )
    at_budget_exhausted = budget.exhausted
    model_with_tools = model if at_budget_exhausted or not tools else model.bind_tools(tools)

    assembled = assemble_context(state_for_context, session_id)

    try:
        return _call_agent_core(
            state=state,
            session_id=session_id,
            model=model,
            model_with_tools=model_with_tools,
            ledger=ledger,
            cache_updates=cache_updates,
            state_for_context=state_for_context,
            budget=budget,
            at_budget_exhausted=at_budget_exhausted,
            assembled=assembled,
            invoke_start_ledger_len=invoke_start_ledger_len,
            config=config,
        )
    except Exception as exc:
        logger.exception("call_agent 未预期异常: %s", exc)
        return _agent_error_payload(
            state,
            budget,
            assembled,
            cache_updates,
            AGENT_FAILURE_MESSAGE,
            invoke_start_ledger_len=invoke_start_ledger_len,
            extra_kwargs={"agent_error": True},
        )


def _call_agent_core(
    *,
    state: AgentState,
    session_id: str | None,
    model,
    model_with_tools,
    ledger: list,
    cache_updates: list,
    state_for_context: dict,
    budget: IterationBudget,
    at_budget_exhausted: bool,
    assembled,
    invoke_start_ledger_len: int,
    config: RunnableConfig | None = None,
) -> dict:
    if assembled.rejected:
        token_updates = _build_token_usage_updates(state, assembled, None)
        rejected_messages: list = list(cache_updates)
        rejected_messages.append(
            AIMessage(
                content=assembled.reject_message or "上下文已满，请新建会话或清理历史。",
                additional_kwargs={"timestamp": int(time.time()), "context_rejected": True},
            )
        )
        return {
            "messages": rejected_messages,
            "step": budget.next_used(),
            "context_summary": assembled.context_summary,
            "compression_count": assembled.compression_count,
            "memory_turn_counter": assembled.memory_turn_counter,
            "memory_nudge_pending": bool(getattr(assembled, "memory_nudge_pending", False)),
            "context_usage": assembled.usage.to_dict(),
            "working_messages": [],
            "last_invoke_ledger_len": _ledger_len_after_invoke(
                invoke_start_ledger_len, rejected_messages, cache_updates
            ),
            **token_updates,
        }

    working = assembled.messages
    emergency_used = False

    # pre_llm_call：仅在轮首（最后一条为真实用户消息，budget.used==0）注入一次，
    # 避免每步重复注入污染上下文、破坏 prompt cache。注入内容为运行时临时消息，
    # 只进本次 LLM 调用、不写入 messages 账本。
    inject_context = None
    if budget.used == 0:
        inject_context = dispatch_pre_llm_call(
            session_id=session_id,
            working_messages=working,
            step=budget.used,
            is_turn_start=True,
        )

    def _with_injection(msgs: list) -> list:
        if not inject_context:
            return msgs
        return list(msgs) + [make_system_message(inject_context, "hook_context")]

    response, llm_error = _invoke_llm(
        model_with_tools, _with_injection(working), session_id, config=config
    )
    if response is None:
        if _is_context_length_error(RuntimeError(llm_error or "")) and not emergency_used:
            emergency_used = True
            assembled = assemble_context(state_for_context, session_id, emergency=True)
            working = assembled.messages
            response, llm_error = _invoke_llm(
                model_with_tools, _with_injection(working), session_id, config=config
            )

        if response is None:
            if _is_context_length_error(RuntimeError(llm_error or "")):
                return _agent_error_payload(
                    state,
                    budget,
                    assembled,
                    cache_updates,
                    "上下文已满，紧急压缩后仍无法继续。请新建会话。",
                    invoke_start_ledger_len=invoke_start_ledger_len,
                    extra_kwargs={"context_rejected": True},
                )
            return _agent_error_payload(
                state,
                budget,
                assembled,
                cache_updates,
                AGENT_FAILURE_MESSAGE,
                invoke_start_ledger_len=invoke_start_ledger_len,
                extra_kwargs={"agent_error": True},
            )

    if isinstance(response, AIMessage):
        response = normalize_ai_message(response)
        response = _strip_duplicate_tool_calls(response, ledger)
        if at_budget_exhausted and _needs_grace_summary(response):
            summary_human, grace_response = _run_grace_summary(model, working, session_id, config)
            usage_dict = assembled.usage.to_dict()
            usage_dict["compressing"] = assembled.compressing
            token_updates = _merge_token_usage_updates(state, assembled, response, grace_response)
            grace_messages: list = list(cache_updates)
            grace_messages.extend([summary_human, grace_response])
            return {
                "messages": grace_messages,
                "step": budget.next_used(),
                "context_summary": assembled.context_summary,
                "compression_count": assembled.compression_count,
                "memory_turn_counter": assembled.memory_turn_counter,
            "memory_nudge_pending": bool(getattr(assembled, "memory_nudge_pending", False)),
                "context_usage": usage_dict,
                "working_messages": working,
                "last_invoke_ledger_len": _ledger_len_after_invoke(
                    invoke_start_ledger_len, grace_messages, cache_updates
                ),
                **token_updates,
            }
        if "timestamp" not in response.additional_kwargs:
            response.additional_kwargs["timestamp"] = int(time.time())
        if ledger and is_system_maintenance(ledger[-1]):
            response.additional_kwargs["system_maintenance"] = True

    usage_dict = assembled.usage.to_dict()
    usage_dict["compressing"] = assembled.compressing
    token_updates = _build_token_usage_updates(state, assembled, response)

    out_messages: list = list(cache_updates)
    out_messages.append(response)

    return {
        "messages": out_messages,
        "step": budget.next_used(),
        "context_summary": assembled.context_summary,
        "compression_count": assembled.compression_count,
        "memory_turn_counter": assembled.memory_turn_counter,
        "memory_nudge_pending": bool(getattr(assembled, "memory_nudge_pending", False)),
        "context_usage": usage_dict,
        "working_messages": working,
        "last_invoke_ledger_len": _ledger_len_after_invoke(
            invoke_start_ledger_len, out_messages, cache_updates
        ),
        **token_updates,
    }
