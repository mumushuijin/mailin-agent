"""Independent format gates must reject data without rewriting its source."""
from __future__ import annotations

import copy
import hashlib
import json
import uuid

import pytest
from langchain_core.messages import HumanMessage

from app.agent.state import CHECKPOINT_SCHEMA_VERSION, new_agent_state, validate_agent_state
from app.storage.conversation_files import SessionConversationFiles
from app.storage.history_projection import HistoryProjectionStore, PROJECTION_VERSION
from app.storage.ledger_contract import LEDGER_SCHEMA_VERSION
from app.storage.model_window import MODEL_WINDOW_SCHEMA_VERSION, ModelWindowStore

SID = str(uuid.uuid4())


@pytest.mark.parametrize("version", [2, LEDGER_SCHEMA_VERSION + 1])
def test_ledger_container_version_gate_preserves_metadata_and_ledger(tmp_path, version):
    files = SessionConversationFiles(tmp_path)
    directory = files.session_dir(SID)
    directory.mkdir()
    files.metadata_path(SID).write_text(json.dumps({
        "schema_version": version,
        "workspace_id": files.workspace_id,
        "session_id": SID,
        "resource_layout": "session-v1",
    }), encoding="utf-8")
    files.ledger_path(SID).write_text('{"seq":1,"message":{"role":"user","content":"old"}}\n', encoding="utf-8")
    before = {path.name: path.read_bytes() for path in directory.iterdir()}

    with pytest.raises(ValueError, match="unsupported ledger schema version"):
        files.read_events(SID)
    with pytest.raises(ValueError, match="unsupported ledger schema version"):
        files.append_messages(SID, "turn-new", [HumanMessage(content="new")])

    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


def test_new_session_gets_current_metadata_without_converting_old_session(tmp_path):
    files = SessionConversationFiles(tmp_path)
    old_session_id = str(uuid.uuid4())
    old_directory = files.session_dir(old_session_id)
    old_directory.mkdir()
    legacy = old_directory / "ledger.jsonl"
    legacy.write_bytes(b'{"seq":1,"message":{"content":"old"}}\n')
    original = legacy.read_bytes()

    metadata = files.ensure_metadata(SID)
    files.append_messages(SID, "turn-new", [HumanMessage(content="new")])

    assert metadata["schema_version"] == LEDGER_SCHEMA_VERSION
    assert files.read_events(SID)[0]["schema_version"] == LEDGER_SCHEMA_VERSION
    assert legacy.read_bytes() == original
    assert not files.metadata_path(old_session_id).exists()


@pytest.mark.parametrize("version", [2, CHECKPOINT_SCHEMA_VERSION + 1, "3"])
def test_checkpoint_schema_gate_rejects_without_mutating_state(version):
    state = new_agent_state(workspace_id="w", session_id=SID, message=HumanMessage(content="hello"))
    state["schema_version"] = version
    original = copy.deepcopy(state)

    with pytest.raises(ValueError, match="unsupported checkpoint schema version"):
        validate_agent_state(state)

    assert state == original


@pytest.mark.parametrize("version", [3, PROJECTION_VERSION + 1])
def test_projection_schema_gate_preserves_unsupported_cache(tmp_path, version):
    store = HistoryProjectionStore(tmp_path)
    path = store.path_for(SID)
    path.parent.mkdir()
    path.write_text(json.dumps({"version": version, "session_id": SID, "messages": []}), encoding="utf-8")
    original = path.read_bytes()

    with pytest.raises(ValueError, match="投影格式"):
        store.read(SID)

    assert path.read_bytes() == original


@pytest.mark.parametrize("version", [0, MODEL_WINDOW_SCHEMA_VERSION + 1, "1"])
def test_model_window_schema_gate_preserves_unsupported_resource(tmp_path, version):
    files = SessionConversationFiles(tmp_path)
    store = ModelWindowStore(files)
    fingerprint = hashlib.sha256(b"").hexdigest()
    current = store.save(
        SID,
        [HumanMessage(content="hello")],
        source_event_cursor={"branch_id": "main", "event_seq": 0, "event_id": "origin"},
        window_schema_version="working-window-v1",
        compression_version="four-layer-v1",
        bootstrap_fingerprint=fingerprint,
        reference_fingerprint=fingerprint,
    )
    document = json.loads(files.read_reference(SID, current.reference))
    document["schema_version"] = version
    data = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    branch_hash = hashlib.sha256(b"main").hexdigest()[:16]
    document_hash = hashlib.sha256(data).hexdigest()
    reference = files.write_content(
        SID, f"content/windows/{branch_hash}-{document_hash}.json", data, media_type="application/json",
    )

    with pytest.raises(ValueError, match="unsupported model window schema version"):
        store.load(SID, reference)

    assert files.read_reference(SID, reference) == data
    assert store.load(SID, current.reference).messages == current.messages
