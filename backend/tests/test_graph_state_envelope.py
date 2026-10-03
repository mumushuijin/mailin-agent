from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent.graph import make_initial_state, _messages_for_ledger_persist
from app.agent.graph import _activate_todo_task


def test_initial_graph_state_contains_canonical_checkpoint_shape():
    state = make_initial_state("hello", 48, session_id="00000000-0000-4000-8000-000000000001")
    assert {"checkpoint_id", "scope", "context", "max_step_every_run", "tasks", "current_task", "current_step", "memory"}.issubset(state)
    assert state["context"]["working_message"]
    assert state["max_step_every_run"] == 48
    assert state["current_step"]["step_status"] == "pending"


def test_todo_transition_creates_stable_task_id_and_keeps_completed_tasks():
    state = {
        "tasks": ["task_existing"],
        "task_details": {
            "task_existing": {"status": "completed"},
            "_runtime": {"todos": [
                {"id": "todo-1", "content": "first", "status": "completed"},
                {"id": "todo-2", "content": "second", "status": "in_progress"},
            ]},
        },
    }
    scope = {"task_id": "task_existing"}
    _activate_todo_task(state, scope)

    assert scope["task_id"] == "task_todo-2"
    assert state["current_task"] == "task_todo-2"
    assert state["tasks"] == ["task_existing", "task_todo-1", "task_todo-2"]
    assert state["task_details"]["task_existing"]["status"] == "completed"


def test_ledger_append_payload_excludes_rebuilt_prompt_and_keeps_new_turn_messages():
    system = SystemMessage(content="system prompt")
    human = HumanMessage(content="question")
    assistant = AIMessage(content="", tool_calls=[{"id": "call-1", "name": "read_file", "args": {}}])
    tool = ToolMessage(content="file contents", tool_call_id="call-1")
    answer = AIMessage(content="done")

    persisted = _messages_for_ledger_persist(
        {
            "working_messages": [system, human, assistant],
            "messages": [assistant, tool, answer],
        },
        include_prompt_system_messages=True,
    )

    assert persisted == [assistant, tool, answer]
