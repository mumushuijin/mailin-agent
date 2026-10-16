import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.storage.conversation_files import INLINE_CONTENT_BYTES, SessionConversationFiles
from app.storage.ledger_contract import validate_row

SID = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"


def message(content):
    return {"role": "user", "content": content}


def test_idempotency_returns_original_seq_and_conflicts_fail(tmp_path):
    files = SessionConversationFiles(tmp_path)
    with pytest.raises(ValueError):
        files.session_dir("../../escape")
    assert files.append(SID, "one", message("hello"), turn_id="t") == (1, True)
    assert files.append(SID, "two", message("world"), turn_id="t") == (2, True)
    assert files.append(SID, "one", message("hello"), turn_id="t") == (1, False)
    for content, turn in (("changed", "t"), ("hello", "other")):
        with pytest.raises(ValueError, match="conflict"):
            files.append(SID, "one", message(content), turn_id=turn)
    assert files.row_at(SID, 1)["message_id"] == "one"
    assert files.row_at(SID, 3) is None
    assert files.read_tail(SID, after_seq=1, offset=999)[0]["seq"] == 2


def test_missing_and_repeated_vendor_ids_remain_distinct(tmp_path):
    files = SessionConversationFiles(tmp_path)
    messages = [AIMessage(content="same", id="run--vendor"), AIMessage(content="same", id="run--vendor"), HumanMessage(content="same")]
    files.append_messages(SID, "t", messages)
    files.append_messages(SID, "t", messages)
    rows = files.read(SID)
    assert len(rows) == len({r["message_id"] for r in rows}) == 3
    assert [r["message"].get("id") for r in rows] == ["run--vendor", "run--vendor", None]
    assert all(r["timestamp"] > 1_000_000_000_000 and "scope" not in r and "origin" not in r for r in rows)
    files.append_messages(SID, "t", files.messages(SID))
    assert files.latest_seq(SID) == 3


def test_content_calls_invalid_parameters_usage_extensions_roundtrip(tmp_path):
    files = SessionConversationFiles(tmp_path)
    content = [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "https://example.test/image"}}, {"type": "vendor_unknown", "payload": {"nested": [1, 2]}}]
    original = AIMessage(content=content, id="run--original", tool_calls=[{"id": "c", "name": "read_file", "args": {"path": "a.txt"}}],
                         invalid_tool_calls=[{"id": "bad", "name": "broken", "args": "{bad json", "error": "invalid", "type": "invalid_tool_call"}],
                         usage_metadata={"input_tokens": 3, "output_tokens": 0, "total_tokens": 3, "output_token_details": {"reasoning": 0}},
                         additional_kwargs={"request_id": "request1", "vendor_extension": {"enabled": True}}, response_metadata={"finish_reason": "tool_calls"})
    files.append_messages(SID, "t", [original])
    row = files.read(SID)[0]
    assert row["message"]["content"] == content
    assert row["message"]["tool_calls"] == [{"id": "c", "name": "read_file", "arguments": {"path": "a.txt"}}]
    assert "total_tokens" not in row["model"]["usage"]
    assert row["model"]["usage"]["output_token_details"]["reasoning"] == 0
    restored = files.messages(SID)[0]
    assert (restored.id, restored.content, restored.tool_calls, restored.invalid_tool_calls, restored.usage_metadata) == (original.id, original.content, original.tool_calls, original.invalid_tool_calls, original.usage_metadata)
    assert restored.additional_kwargs["vendor_extension"] == {"enabled": True}
    files.append_messages(SID, "t", [restored])
    assert files.latest_seq(SID) == 1


def test_metadata_versions_containers_and_old_data_fail_closed(tmp_path):
    files = SessionConversationFiles(tmp_path)
    with pytest.raises(ValueError, match="metadata"):
        files.read(SID)
    files.ensure_metadata(SID)
    assert files.read_metadata(SID) == {"schema_version": 2, "workspace_id": files.workspace_id, "session_id": SID}
    with pytest.raises(ValueError, match="identity"):
        SessionConversationFiles(tmp_path, "different").read(SID)
    meta = files.metadata_path(SID)
    meta.write_text(json.dumps({"schema_version": 99, "workspace_id": files.workspace_id, "session_id": SID}))
    with pytest.raises(ValueError, match="version"):
        files.append(SID, "one", message("hello"), turn_id="t")
    assert json.loads(meta.read_text())["schema_version"] == 99
    old = files.ledger_path(OTHER)
    old.parent.mkdir()
    old.write_text('{"seq":1,"scope":{"run_id":"old"},"message":{}}\n')
    before = old.read_bytes()
    with pytest.raises(ValueError, match="metadata"):
        files.read(OTHER)
    with pytest.raises(ValueError, match="metadata"):
        files.append(OTHER, "new", message("new"), turn_id="t")
    assert old.read_bytes() == before and not files.metadata_path(OTHER).exists()


@pytest.mark.parametrize("damage", ["bad-json", "wrong-seq", "duplicate-id", "old-scope"])
def test_complete_line_damage_is_not_rewritten(tmp_path, damage):
    files = SessionConversationFiles(tmp_path)
    files.append(SID, "one", message("hello"), turn_id="t")
    row = files.read(SID)[0]
    damaged = {**row, "seq": 4 if damage == "wrong-seq" else 2}
    if damage == "old-scope":
        damaged["scope"] = {"run_id": "old"}
    with files.ledger_path(SID).open("a") as stream:
        stream.write("broken\n" if damage == "bad-json" else json.dumps(damaged) + "\n")
    before = files.ledger_path(SID).read_bytes()
    with pytest.raises(ValueError):
        SessionConversationFiles(tmp_path).read(SID)
    assert files.ledger_path(SID).read_bytes() == before


def test_unterminated_crash_tail_is_repaired(tmp_path):
    files = SessionConversationFiles(tmp_path)
    files.append(SID, "one", message("hello"), turn_id="t")
    with files.ledger_path(SID).open("ab") as stream:
        stream.write(b'{"seq":2,"message_id":"torn"')
    restarted = SessionConversationFiles(tmp_path)
    assert len(restarted.read(SID)) == 1
    assert restarted.append(SID, "two", message("after"), turn_id="t") == (2, True)


def test_parallel_duplicate_submissions_share_original_sequence(tmp_path):
    files = [SessionConversationFiles(tmp_path) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda n: files[n % 2].append(SID, "same", message("same"), turn_id="t"), range(16)))
    assert {seq for seq, _ in results} == {1}
    assert sum(added for _, added in results) == 1
    files[1].append(SID, "next", message("next"), turn_id="t")
    assert files[0].latest_seq(SID) == 2


def test_full_reference_integrity_export_and_delete_isolation(tmp_path):
    files = SessionConversationFiles(tmp_path)
    full = "first\n" + "x" * (INLINE_CONTENT_BYTES + 1)
    files.append_messages(SID, "t", [ToolMessage(content=full, tool_call_id="c")])
    reference = files.read(SID)[0]["message"]["content"][0]
    assert files.read_reference(SID, reference).decode() == files.messages(SID)[0].content == full
    summary = files.write_summary(SID, "s1", {"content": "summary"})
    exported = files.export_files(SID)
    assert {"ledger_meta.json", "ledger.jsonl", reference["path"], summary}.issubset(exported)
    assert "workspace_id" not in json.loads(exported["ledger.jsonl"].splitlines()[0])
    with pytest.raises(ValueError, match="outside"):
        files.write_content(SID, "../escape.txt", "bad")
    files.append(OTHER, "other", message("preserved"), turn_id="t")
    shared = tmp_path / "checkpoint.sqlite"
    shared.write_text("keep")
    resource = files.session_dir(SID) / reference["path"]
    resource.write_text("damaged")
    with pytest.raises(ValueError, match="integrity"):
        files.messages(SID)
    resource.unlink()
    with pytest.raises(ValueError, match="unavailable"):
        files.read(SID)
    files.delete_session(SID)
    assert files.ledger_path(OTHER).exists() and shared.read_text() == "keep"


def test_reference_must_be_complete_before_ledger_commit(tmp_path):
    files = SessionConversationFiles(tmp_path)
    result = {"role": "tool", "tool_call_id": "c", "content": [{"type": "reference", "path": "tool-results/missing.txt", "media_type": "text/plain", "bytes": 1, "sha256": "0" * 64}]}
    with pytest.raises(ValueError, match="unavailable"):
        files.append(SID, "one", result, turn_id="t", execution={"status": "succeeded"})
    assert files.latest_seq(SID) == 0


def test_audit_and_rebuilt_prompts_do_not_enter_model_window(tmp_path):
    files = SessionConversationFiles(tmp_path)
    files.append_messages(SID, "t", [SystemMessage(content="bootstrap", additional_kwargs={"context_bootstrap": True}), AIMessage(content="hello")])
    files.append(SID, "audit", {"role": "system", "content": [{"type": "lifecycle", "subject": "turn"}]}, turn_id="t", execution={"status": "completed"})
    assert len(files.read(SID)) == 2 and [m.content for m in files.messages(SID)] == ["hello"]
    assert len(files.messages(SID, include_audit=True)) == 2


def test_privacy_keeps_zero_usage_but_removes_hidden_reasoning_and_secrets(tmp_path):
    files = SessionConversationFiles(tmp_path)
    original = AIMessage(content=[{"type": "text", "text": "password=abc"}, {"type": "reasoning", "text": "private"}], additional_kwargs={"reasoning_content": "private", "request_id": "req"},
                         tool_calls=[{"id": "c", "name": "shell", "args": {"api_key": "SECRET", "command": "echo ok"}}], usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "output_token_details": {"reasoning": 0}})
    files.append_messages(SID, "t", [original])
    encoded = files.ledger_path(SID).read_text()
    assert "private" not in encoded and "SECRET" not in encoded and "abc" not in encoded
    assert files.read(SID)[0]["model"]["usage"]["output_token_details"]["reasoning"] == 0


def test_summary_missing_and_path_escape_fail_closed(tmp_path):
    files = SessionConversationFiles(tmp_path)
    one = files.write_summary(SID, "s1", {"content": "one"})
    two = files.write_summary(SID, "s2", {"content": "two"})
    assert files.read_summary(SID, one)["content"] == "one"
    assert files.read_summary(SID, two)["content"] == "two"
    with pytest.raises(ValueError):
        files.read_summary(SID, "summaries/missing.json")
    with pytest.raises(ValueError):
        files.read_summary(SID, "../missing.json")


def test_execution_domains_and_duplicate_aliases_are_strict():
    row = {"seq": 1, "message_id": "m", "turn_id": "t", "timestamp": 0, "message": {"role": "tool", "tool_call_id": "c", "content": ""}, "execution": {"status": "succeeded"}}
    validate_row(row)
    with pytest.raises(ValueError, match="tool execution"):
        validate_row({**row, "execution": {"status": "completed"}})
    with pytest.raises(ValueError, match="tool execution"):
        validate_row({**row, "interaction": {"id": "i", "kind": "approval"}, "execution": {"status": "completed"}})
    with pytest.raises(ValueError, match="unexecuted"):
        validate_row({**row, "execution": {"status": "denied", "attempt": 1}})
    with pytest.raises(ValueError):
        validate_row({**row, "scope": {"run_id": "old"}})


def test_index_refresh_reads_only_new_lines_and_offset_cannot_skip_tail(tmp_path, monkeypatch):
    first, second = SessionConversationFiles(tmp_path), SessionConversationFiles(tmp_path)
    first.append(SID, "one", message("hello"), turn_id="t")
    assert second.latest_seq(SID) == 1
    first.append(SID, "two", message("world"), turn_id="t")
    import app.storage.conversation_files as module
    original = module.validate_row
    validated = []
    def counted(row):
        validated.append(row["seq"])
        return original(row)
    monkeypatch.setattr(module, "validate_row", counted)
    assert second.latest_seq(SID) == 2
    assert validated == [2]
    assert second.read_tail(SID, after_seq=1, offset=second.offset_after(SID, 2))[0]["seq"] == 2


@pytest.mark.parametrize("pointer", ["../escape.txt", "/absolute.txt", "C:/escape.txt", "tool-results/../../escape.txt", "tool-results\\escape.txt", "summaries/escape.txt"])
def test_content_reference_paths_are_restricted(tmp_path, pointer):
    with pytest.raises(ValueError):
        SessionConversationFiles(tmp_path).write_content(SID, pointer, "bad")


@pytest.mark.parametrize("domain,status", [("tool", "completed"), ("turn", "succeeded"), ("step", "unknown"), ("model_request", "denied")])
def test_lifecycle_domains_reject_foreign_statuses(domain, status):
    row = {"seq": 1, "message_id": "m", "timestamp": 1, "turn_id": "t", "message": {"role": "system", "content": [{"type": "lifecycle", "subject": domain}]}, "execution": {"status": status}}
    if domain == "tool":
        row["message"]["tool_call_id"] = "c"
    if domain == "model_request":
        row["model"] = {"request_id": "req"}
    with pytest.raises(ValueError, match="execution status"):
        validate_row(row)


def test_reference_inline_conflict_and_wrong_checksum_fail(tmp_path):
    files = SessionConversationFiles(tmp_path)
    reference = files.write_content(SID, "content/data.txt", "complete")
    row = {"seq": 1, "message_id": "m", "timestamp": 1, "turn_id": "t", "message": {"role": "user", "content": [reference, {"type": "text", "text": "preview"}]}}
    with pytest.raises(ValueError, match="mutually exclusive"):
        validate_row(row)
    with pytest.raises(ValueError, match="integrity"):
        files.read_reference(SID, {**reference, "sha256": "0" * 64})


def test_old_summaries_only_session_never_gets_new_metadata(tmp_path):
    files = SessionConversationFiles(tmp_path)
    directory = files.session_dir(SID) / "summaries"
    directory.mkdir(parents=True)
    (directory / "old.json").write_text('{"content":"old"}')
    with pytest.raises(ValueError, match="metadata"):
        files.ensure_metadata(SID)
    assert not files.metadata_path(SID).exists()


def test_original_vendor_identifier_is_never_sanitized_or_reinterpreted(tmp_path):
    files = SessionConversationFiles(tmp_path)
    original_id = "run--password=vendor-identifier"
    files.append_messages(SID, "t", [AIMessage(content="hello", id=original_id)])
    assert files.read(SID)[0]["message"]["id"] == files.messages(SID)[0].id == original_id


def test_summary_maintenance_cannot_claim_missing_content(tmp_path):
    files = SessionConversationFiles(tmp_path)
    with pytest.raises(ValueError, match="不可用"):
        files.append(SID, "m", {"role": "system", "content": [{"type": "lifecycle", "subject": "context"}]},
                     extensions={"context": {"summary_ref": "summaries/missing.json", "covered_seq": [1, 1]}},
                     execution={"status": "completed"})
    assert files.latest_seq(SID) == 0
