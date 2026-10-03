import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, message_to_dict

from app.storage.conversation_files import SessionConversationFiles
from app.storage.ledger_contract import validate_row


def scope(sid, run="run-1"):
    return {"workspace_id": "workspace-1", "session_id": sid, "run_id": run,
            "task_id": "task-1", "request_id": None, "step_id": None}


def encoded(content):
    return message_to_dict(HumanMessage(content=content))


def test_append_only_ledger_is_idempotent_and_rejects_invalid_session(tmp_path):
    store = SessionConversationFiles(tmp_path)
    sid = "00000000-0000-4000-8000-000000000001"
    with pytest.raises(ValueError):
        store.session_dir("../../escape")
    first = store.append(sid, "m1", encoded("hello"), scope=scope(sid), origin="user")
    duplicate = store.append(sid, "m1", encoded("hello"), scope=scope(sid), origin="user")
    second = store.append(sid, "m2", encoded("world"), scope=scope(sid), origin="user")
    assert first == (1, True)
    assert duplicate == (1, False)
    assert second == (2, True)
    rows = store.read(sid)
    assert [row["message_id"] for row in rows] == ["m1", "m2"]
    assert [row["message_id"] for row in store.read(sid, after_seq=1)] == ["m2"]
    assert store.read(sid, after_seq=2) == []
    assert [row["message_id"] for row in store.read_tail(sid, after_seq=1)] == ["m2"]
    offset = store.offset_after(sid, 1)
    assert [row["message_id"] for row in store.read_tail(sid, after_seq=1, offset=offset)] == ["m2"]
    assert json.loads(store.ledger_path(sid).read_text().splitlines()[0])["seq"] == 1


def test_append_messages_assigns_stable_ids(tmp_path):
    store = SessionConversationFiles(tmp_path)
    sid = "00000000-0000-4000-8000-000000000002"
    store.append_messages(sid, "run-1", [HumanMessage(content="hello")], scope=scope(sid))
    store.append_messages(sid, "run-1", [HumanMessage(content="hello")], scope=scope(sid))
    assert len(store.read(sid)) == 1


def test_new_rows_keep_vendor_id_separate_and_reject_missing_scope(tmp_path):
    store = SessionConversationFiles(tmp_path)
    sid = "00000000-0000-4000-8000-000000000011"
    assistant = AIMessage(content="answer", id="run--vendor-message")
    store.append_messages(sid, "actual-run", [assistant], scope=scope(sid, "actual-run"))
    row = store.read(sid)[0]
    assert row["scope"]["run_id"] == "actual-run"
    assert row["origin"] == "assistant"
    assert row["message_id"] != "run--vendor-message"
    assert row["message"]["data"]["id"] == "run--vendor-message"
    assert set(row) == {"seq", "message_id", "scope", "origin", "message"}
    bad = {key: value for key, value in row.items() if key != "scope"}
    store.ledger_path(sid).write_text(json.dumps(bad) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="格式无效"):
        SessionConversationFiles(tmp_path).read(sid)


def test_projection_read_deduplicates_repeated_message_id(tmp_path):
    store = SessionConversationFiles(tmp_path)
    sid = "00000000-0000-4000-8000-000000000014"
    store.append_messages(sid, "run-1", [HumanMessage(content="hello")], scope=scope(sid))
    row = store.read(sid)[0]
    with store.ledger_path(sid).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({**row, "seq": 2}) + "\n")
    assert len(SessionConversationFiles(tmp_path).read(sid)) == 1


def test_backend_documentation_ledger_example_uses_contract_fields():
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    example = readme.split("例如：\n\n```json\n", 1)[1].split("\n```", 1)[0]
    row = validate_row(json.loads(example))
    assert row["scope"]["request_id"] is None
    assert row["origin"] == "user"


def test_rebuilt_system_prompt_is_not_a_ledger_event(tmp_path):
    store = SessionConversationFiles(tmp_path)
    sid = "00000000-0000-4000-8000-000000000012"
    for index in range(3):
        store.append_messages(sid, "run-1", [
            SystemMessage(content="bootstrap", additional_kwargs={"context_bootstrap": True}),
            AIMessage(content=f"answer {index}"),
        ], scope={**scope(sid), "step_id": f"step-{index}"})
    assert [row["origin"] for row in store.read(sid)] == ["assistant"] * 3


def test_explicit_maintenance_event_has_its_own_origin(tmp_path):
    store = SessionConversationFiles(tmp_path)
    sid = "00000000-0000-4000-8000-000000000013"
    maintenance = SystemMessage(content="configuration changed", additional_kwargs={"system_maintenance": True, "kind": "config_change"})
    store.append_messages(sid, "run-1", [maintenance], scope=scope(sid))
    assert store.read(sid)[0]["origin"] == "system_maintenance"


def test_separate_instances_share_append_sequence_and_tail_index(tmp_path):
    first = SessionConversationFiles(tmp_path)
    second = SessionConversationFiles(tmp_path)
    sid = "00000000-0000-4000-8000-000000000008"

    first.append_messages(sid, "run-1", [HumanMessage(content="first", id="m1")], scope=scope(sid, "run-1"))
    second.append_messages(sid, "run-2", [HumanMessage(content="second", id="m2")], scope=scope(sid, "run-2"))
    first.append_messages(sid, "run-3", [HumanMessage(content="third", id="m3")], scope=scope(sid, "run-3"))

    rows = second.read(sid)
    assert [row["seq"] for row in rows] == [1, 2, 3]
    offset = first.offset_after(sid, 2)
    assert [row["message"]["data"]["content"] for row in first.read_tail(sid, after_seq=2, offset=offset)] == ["third"]


def test_old_format_rows_are_rejected(tmp_path):
    store = SessionConversationFiles(tmp_path)
    sid = "00000000-0000-4000-8000-000000000009"
    path = store.ledger_path(sid)
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"seq":1,"message_id":"m1","message":{"content":"one"}}\n'
        '{"seq":1,"message_id":"m2","message":{"content":"two"}}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="格式无效"):
        store.read(sid)
    with pytest.raises(ValueError, match="格式无效"):
        store.append(sid, "m3", encoded("three"), scope=scope(sid), origin="user")


def test_deleting_one_session_preserves_other_session_files(tmp_path):
    store = SessionConversationFiles(tmp_path)
    first = "00000000-0000-4000-8000-000000000003"
    second = "00000000-0000-4000-8000-000000000004"
    store.append(first, "m1", encoded("one"), scope=scope(first), origin="user")
    store.append(second, "m2", encoded("two"), scope=scope(second), origin="user")
    store.write_summary(second, "s1", {"content": "summary"})
    store.delete_session(first)
    assert not store.session_dir(first).exists()
    assert store.ledger_path(second).exists()
    assert store.summary_path(second, "s1").exists()


def test_summary_pointer_is_resolved_and_missing_summary_fails_closed(tmp_path):
    store = SessionConversationFiles(tmp_path)
    sid = "00000000-0000-4000-8000-000000000005"
    pointer = store.write_summary(sid, "summary-1", {"content": "durable summary"})
    restarted = SessionConversationFiles(tmp_path)
    assert restarted.read_summary(sid, pointer)["content"] == "durable summary"
    with pytest.raises(ValueError, match="不可用"):
        restarted.read_summary(sid, "summaries/missing.json")


def test_successive_compression_summaries_keep_independent_pointers(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from langchain_core.messages import HumanMessage

    from app.agent.state import new_agent_state, to_runtime_view

    sid = "00000000-0000-4000-8000-000000000007"
    store = SessionConversationFiles(tmp_path)
    first = store.write_summary(sid, "summary-1", {"summary_id": "summary-1", "content": "first window"})
    second = store.write_summary(sid, "summary-2", {"summary_id": "summary-2", "content": "merged window"})
    monkeypatch.setattr("app.core.settings.get_settings", lambda: SimpleNamespace(workspace_path=tmp_path))
    state = new_agent_state(
        workspace_id="workspace", session_id=sid, message=HumanMessage(content="current"),
    )
    state["context"]["compression_count"] = 2
    state["context"]["summary_pointer"] = second

    assert store.read_summary(sid, first)["content"] == "first window"
    assert to_runtime_view(state)["context_summary"] == "merged window"
    assert state["context"]["window_size_tokens"] > 0


def test_append_recovers_an_unterminated_crash_fragment_before_next_message(tmp_path):
    store = SessionConversationFiles(tmp_path)
    sid = "00000000-0000-4000-8000-000000000006"
    store.append(sid, "m1", encoded("durable"), scope=scope(sid), origin="user")
    with store.ledger_path(sid).open("ab") as stream:
        stream.write(b'{"seq":2,"message_id":"torn"')

    restarted = SessionConversationFiles(tmp_path)
    assert restarted.append(sid, "m2", encoded("recovered"), scope=scope(sid), origin="user") == (2, True)
    assert [row["message_id"] for row in restarted.read(sid)] == ["m1", "m2"]
