from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.nodes.router import effective_step, should_continue
from app.context.engine import assemble_context
from app.context.ledger import repair_orphan_tool_calls
from app.storage.workspace import normalize_workspace_path


def test_effective_step_resets_on_new_human_message():
    state = {
        "step": 6,
        "max_steps": 6,
        "messages": [HumanMessage(content="新问题")],
    }
    assert effective_step(state) == 0


def test_effective_step_continues_during_tool_loop():
    state = {
        "step": 2,
        "max_steps": 6,
        "messages": [
            HumanMessage(content="hi"),
            AIMessage(content="", tool_calls=[{"id": "c1", "name": "read_file", "args": {}}]),
            ToolMessage(content="ok", tool_call_id="c1"),
        ],
    }
    assert effective_step(state) == 2


def test_router_routes_to_tools_when_pending_tool_calls():
    state = {
        "step": 6,
        "max_steps": 6,
        "messages": [
            HumanMessage(content="hi"),
            AIMessage(
                content="",
                tool_calls=[{"id": "c1", "name": "list_directory", "args": {}}],
            ),
        ],
    }
    assert should_continue(state) == "tools"


def test_repair_orphan_tool_calls_backfills_tool_messages():
    ai = AIMessage(
        content="",
        tool_calls=[{"id": "c1", "name": "read_file", "args": {"file_path": "a.txt"}}],
    )
    messages = [HumanMessage(content="q"), ai]
    repaired = repair_orphan_tool_calls(messages)
    assert len(repaired) == 3
    assert isinstance(repaired[2], ToolMessage)
    assert repaired[2].tool_call_id == "c1"


def test_assemble_keeps_previous_turn_final_answer():
    state = {
        "messages": [
            HumanMessage(content="你好"),
            AIMessage(content="我是麦林"),
            HumanMessage(content="工作区有什么"),
            AIMessage(
                content="",
                tool_calls=[{"id": "c1", "name": "list_directory", "args": {}}],
            ),
            ToolMessage(content="[dir] artifacts", tool_call_id="c1", name="list_directory"),
            AIMessage(content="当前工作区结构如下：artifacts/ bootstraps/"),
            HumanMessage(content="你可以修改自己的配置吗"),
        ],
        "step": 6,
        "max_steps": 6,
        "context_summary": "",
        "compression_count": 0,
        "memory_turn_counter": 0,
    }
    assembled = assemble_context(state)
    body = [m for m in assembled.messages if not hasattr(m, "type") or m.type != "system"]
    contents = []
    for m in assembled.messages:
        if isinstance(m, HumanMessage):
            contents.append(("human", m.content))
        elif isinstance(m, AIMessage):
            contents.append(("ai", m.content))
    assert any(
        isinstance(m, AIMessage) and "当前工作区结构如下" in str(m.content)
        for m in assembled.messages
    )
    for i, msg in enumerate(assembled.messages):
        if isinstance(msg, AIMessage) and msg.tool_calls:
            ids = {tc.get("id") for tc in msg.tool_calls if tc.get("id")}
            paired: set[str] = set()
            j = i + 1
            while j < len(assembled.messages) and isinstance(assembled.messages[j], ToolMessage):
                paired.add(assembled.messages[j].tool_call_id)
                j += 1
            assert ids.issubset(paired), "工作视图中的 tool_calls 应有配对的 ToolMessage"


def test_normalize_workspace_path_dot_and_empty_mean_root():
    assert normalize_workspace_path(".") == ""
    assert normalize_workspace_path("") == ""
    assert normalize_workspace_path("./") == ""
    assert normalize_workspace_path("./bootstraps") == "bootstraps"
