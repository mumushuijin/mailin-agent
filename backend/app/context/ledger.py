from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.agent.messages import is_real_user_message
from app.context.budget import count_message_tokens, load_context_config


def get_ledger_messages(state: dict) -> list[BaseMessage]:
    """从 AgentState 读取会话账本（checkpoint 中的 messages 字段）。"""
    return list(state.get("messages") or [])


def split_turns(messages: list[BaseMessage]) -> list[list[BaseMessage]]:
    """将账本按用户轮次切分。

    一轮 = 用户发消息起，到助手给出最终文字回复（可含多轮工具调用）止。
    每条 HumanMessage 开启新一轮。
    """
    turns: list[list[BaseMessage]] = []
    current: list[BaseMessage] = []
    for msg in messages:
        if isinstance(msg, HumanMessage) and is_real_user_message(msg) and current:
            turns.append(current)
            current = [msg]
        else:
            current.append(msg)
    if current:
        turns.append(current)
    return turns


def get_recent_turns(messages: list[BaseMessage], max_pairs: int) -> list[BaseMessage]:
    """保留最近 N 轮 user-assistant 对话（含工具链）。"""
    turns = split_turns(messages)
    if max_pairs <= 0:
        return list(messages)
    return [msg for turn in turns[-max_pairs:] for msg in turn]


def get_current_user_message(messages: list[BaseMessage]) -> HumanMessage | None:
    for msg in reversed(messages):
        if is_real_user_message(msg):
            return msg
    return None


def find_last_complete_tool_round(messages: list[BaseMessage]) -> set[str]:
    """定位最近一条 ReAct 工具链（单次 tool_calls + ToolMessage）的消息 id。

    注意：这是「工具调用链」，不等于用户对话轮；Layer2 应使用 is_current_user_turn。
    """
    protected_ids: set[str] = set()
    last_ai_with_tools: AIMessage | None = None
    for msg in reversed(messages):
        if isinstance(msg, ToolMessage):
            if msg.id:
                protected_ids.add(msg.id)
        elif isinstance(msg, AIMessage) and msg.tool_calls:
            last_ai_with_tools = msg
            break
    if last_ai_with_tools and last_ai_with_tools.id:
        protected_ids.add(last_ai_with_tools.id)
        for tc in last_ai_with_tools.tool_calls:
            tc_id = tc.get("id")
            if tc_id:
                for m in messages:
                    if isinstance(m, ToolMessage) and m.tool_call_id == tc_id and m.id:
                        protected_ids.add(m.id)
    return protected_ids


def messages_before_current_turn(messages: list[BaseMessage]) -> list[BaseMessage]:
    """当前用户消息之前的所有消息。"""
    last_human_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if is_real_user_message(messages[i]):
            last_human_idx = i
            break
    if last_human_idx <= 0:
        return []
    return messages[:last_human_idx]


def current_turn_object_ids(messages: list[BaseMessage]) -> set[int]:
    """当前用户轮内所有消息的对象 id（从最后一条 User 消息起到账本末尾）。"""
    last_human_idx = -1
    for i, msg in enumerate(messages):
        if is_real_user_message(msg):
            last_human_idx = i
    if last_human_idx < 0:
        return set()
    return {id(m) for m in messages[last_human_idx:]}


def is_current_user_turn(msg: BaseMessage, messages: list[BaseMessage]) -> bool:
    """是否属于当前用户轮（末条 User 消息之后、下一条 User 消息之前）。"""
    return id(msg) in current_turn_object_ids(messages)


def is_in_current_turn(msg: BaseMessage, messages: list[BaseMessage]) -> bool:
    return is_current_user_turn(msg, messages)


def has_pending_tool_calls(messages: list[BaseMessage]) -> bool:
    """末条消息是否为尚未执行完成的 tool_calls。"""
    if not messages:
        return False
    last = messages[-1]
    return isinstance(last, AIMessage) and bool(last.tool_calls)


def cancel_tool_calls_message(
    ai: AIMessage,
    *,
    reason: str = "未执行：工具调用已取消。",
) -> list[ToolMessage]:
    """为 AIMessage 上每个 tool_call 生成配对的取消型 ToolMessage。"""
    cancelled: list[ToolMessage] = []
    for tc in ai.tool_calls:
        tc_id = tc.get("id")
        if not tc_id:
            continue
        cancelled.append(
            ToolMessage(
                content=reason,
                tool_call_id=tc_id,
                name=tc.get("name"),
            )
        )
    return cancelled


def split_tail_window(
    messages: list[BaseMessage],
    max_tail_tokens: int | None = None,
) -> tuple[list[BaseMessage], list[BaseMessage]]:
    """按 token 从账本末尾切出尾部窗口，返回 (middle, tail)。"""
    if not messages:
        return [], []

    if max_tail_tokens is None:
        config = load_context_config()
        max_tail_tokens = int(config.get("recent_tail_max_tokens", 20_000))

    tail: list[BaseMessage] = []
    tokens = 0
    for msg in reversed(messages):
        msg_tokens = count_message_tokens(msg)
        if tail and tokens + msg_tokens > max_tail_tokens:
            break
        tail.insert(0, msg)
        tokens += msg_tokens

    split_idx = len(messages) - len(tail)
    return messages[:split_idx], tail


def repair_orphan_tool_calls(
    messages: list[BaseMessage],
    *,
    reason: str = "未执行：历史工具调用未完成（可能因步数截断）。",
) -> list[BaseMessage]:
    """修补账本中「有 tool_calls、无对应 ToolMessage」的畸形记录。"""
    if not messages:
        return messages

    answered_ids: set[str] = set()
    for msg in messages:
        if isinstance(msg, ToolMessage) and msg.tool_call_id:
            answered_ids.add(msg.tool_call_id)

    repaired: list[BaseMessage] = []
    changed = False
    for i, msg in enumerate(messages):
        repaired.append(msg)
        if not isinstance(msg, AIMessage) or not msg.tool_calls:
            continue

        pending = [tc for tc in msg.tool_calls if tc.get("id") and tc.get("id") not in answered_ids]
        if not pending:
            continue

        following = messages[i + 1 :]
        matched = 0
        for follow in following:
            if isinstance(follow, AIMessage):
                break
            if isinstance(follow, ToolMessage) and follow.tool_call_id in {
                tc.get("id") for tc in pending
            }:
                matched += 1
            elif not isinstance(follow, ToolMessage):
                break

        if matched >= len(pending):
            for tc in pending:
                answered_ids.add(tc["id"])
            continue

        for tc in pending:
            tc_id = tc.get("id")
            if not tc_id:
                continue
            repaired.append(
                ToolMessage(
                    content=reason,
                    tool_call_id=tc_id,
                    name=tc.get("name"),
                )
            )
            answered_ids.add(tc_id)
            changed = True

    return repaired if changed else messages
