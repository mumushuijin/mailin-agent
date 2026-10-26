from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.state import merge_agent_state, new_agent_state
from app.storage.conversation_files import SessionConversationFiles
from app.storage.model_window import (
    ModelWindowStore,
    StaleModelWindowError,
)

SID = str(uuid.uuid4())
BOOTSTRAP = hashlib.sha256(b"bootstrap").hexdigest()
REFERENCES = hashlib.sha256(b"references").hexdigest()


def _save(store: ModelWindowStore, messages=None, **overrides):
    return store.save(
        SID,
        messages or [HumanMessage(content="hello"), AIMessage(content="world")],
        source_event_cursor={"branch_id": "main", "event_seq": 2, "event_id": "evt-2"},
        window_schema_version="working-window-v1",
        compression_version="four-layer-v1",
        bootstrap_fingerprint=BOOTSTRAP,
        reference_fingerprint=REFERENCES,
        **overrides,
    )


def test_window_is_saved_separately_and_round_trips_with_provenance(tmp_path):
    files = SessionConversationFiles(tmp_path)
    store = ModelWindowStore(files)

    saved = _save(store)
    loaded = store.load(
        SID,
        saved.reference,
        expected_source_event_cursor=saved.source_event_cursor,
        expected_bootstrap_fingerprint=BOOTSTRAP,
        expected_reference_fingerprint=REFERENCES,
        expected_window_schema_version="working-window-v1",
        expected_compression_version="four-layer-v1",
    )

    assert saved.reference["path"].startswith("content/windows/")
    assert loaded.messages == saved.messages
    assert loaded.content_sha256 == saved.content_sha256
    assert loaded.branch_id == "main"
    assert loaded.source_event_cursor == {"branch_id": "main", "event_seq": 2, "event_id": "evt-2"}
    assert files.latest_seq(SID) == 0


def test_control_cursor_can_advance_without_changing_window_resource(tmp_path):
    store = ModelWindowStore(SessionConversationFiles(tmp_path))
    first = _save(store)
    checkpoint = new_agent_state(workspace_id="w", session_id=SID, message=HumanMessage(content="hello"))
    checkpoint["context"]["window_reference"] = first.reference
    checkpoint["context"]["source_event_cursor"] = first.source_event_cursor
    checkpoint["context"]["content_sha256"] = first.content_sha256
    later = merge_agent_state(checkpoint, {
        "state_revision": 1,
        "ledger_pointer": {
            "applied_seq": 3,
            "event_cursor": {"branch_id": "main", "event_seq": 3, "event_id": "evt-control"},
        },
    })

    assert later["ledger_pointer"]["event_cursor"]["event_seq"] == 3
    assert later["context"]["window_reference"] == first.reference
    assert later["context"]["source_event_cursor"] == first.source_event_cursor
    assert later["context"]["content_sha256"] == first.content_sha256
    assert store.load(SID, first.reference).messages == first.messages


@pytest.mark.parametrize(
    "expected",
    [
        {"expected_source_event_cursor": {"branch_id": "main", "event_seq": 4, "event_id": "evt-4"}},
        {"expected_bootstrap_fingerprint": hashlib.sha256(b"new bootstrap").hexdigest()},
        {"expected_reference_fingerprint": hashlib.sha256(b"new refs").hexdigest()},
        {"expected_compression_version": "different-v2"},
    ],
)
def test_stale_window_is_identified_and_can_be_invalidated(tmp_path, expected):
    store = ModelWindowStore(SessionConversationFiles(tmp_path))
    saved = _save(store)

    with pytest.raises(StaleModelWindowError, match="stale"):
        store.load(SID, saved.reference, **expected)
    assert store.load_if_current(SID, saved.reference, **expected) is None


def test_nested_references_are_verified_and_bounded(tmp_path):
    files = SessionConversationFiles(tmp_path)
    resource = files.write_content(SID, "content/source.txt", b"payload", media_type="text/plain")
    store = ModelWindowStore(files, max_bytes=2048)
    message = {"message": {"role": "assistant", "content": [resource]}}

    saved = _save(store, [message])
    assert store.load(SID, saved.reference).messages == [message]
    with pytest.raises(ValueError, match="integrity"):
        store.save(SID, [{"message": {"role": "assistant", "content": [{**resource, "sha256": "0" * 64}]}}],
                   source_event_cursor={"branch_id": "main", "event_seq": 2, "event_id": "evt-2"},
                   window_schema_version="working-window-v1", compression_version="four-layer-v1",
                   bootstrap_fingerprint=BOOTSTRAP, reference_fingerprint=REFERENCES)

    oversized = files.write_content(SID, "content/large.txt", b"x" * 2049, media_type="text/plain")
    with pytest.raises(ValueError, match="reference exceeds"):
        _save(store, [{"message": {"role": "assistant", "content": [oversized]}}])


def test_window_rejects_invalid_cursor_versions_and_fingerprints(tmp_path):
    store = ModelWindowStore(SessionConversationFiles(tmp_path))
    common = {
        "window_schema_version": "working-window-v1",
        "compression_version": "four-layer-v1",
        "bootstrap_fingerprint": BOOTSTRAP,
        "reference_fingerprint": REFERENCES,
    }
    with pytest.raises(ValueError, match="source_event_cursor"):
        store.save(SID, [], source_event_cursor={"branch_id": "main", "event_seq": 0, "event_id": "evt-1"}, **common)
    with pytest.raises(ValueError, match="fingerprint"):
        store.save(SID, [], source_event_cursor={"branch_id": "main", "event_seq": 0, "event_id": "origin"},
                   **{**common, "bootstrap_fingerprint": "not-a-digest"})
    with pytest.raises(ValueError, match="version identifier"):
        store.save(SID, [], source_event_cursor={"branch_id": "main", "event_seq": 0, "event_id": "origin"},
                   **{**common, "compression_version": "../unknown"})


def test_window_enforces_message_and_serialized_byte_limits(tmp_path):
    files = SessionConversationFiles(tmp_path)
    message_limited = ModelWindowStore(files, max_messages=1)
    with pytest.raises(ValueError, match="message limit"):
        _save(message_limited)

    byte_limited = ModelWindowStore(files, max_bytes=256)
    with pytest.raises(ValueError, match="byte limit"):
        _save(byte_limited, [HumanMessage(content="x" * 1000)])


def test_corrupt_inner_document_hash_is_rejected(tmp_path):
    files = SessionConversationFiles(tmp_path)
    store = ModelWindowStore(files)
    saved = _save(store)
    raw = files.read_reference(SID, saved.reference)
    document = json.loads(raw)
    document["content_sha256"] = "0" * 64
    tampered = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    branch_identity = hashlib.sha256(b"main").hexdigest()[:16]
    content_identity = hashlib.sha256(tampered).hexdigest()
    replacement = files.write_content(
        SID,
        f"content/windows/{branch_identity}-{content_identity}.json",
        tampered,
        media_type="application/json",
    )

    with pytest.raises(ValueError, match="content hash integrity"):
        store.load(SID, replacement)


@pytest.mark.parametrize("pointer", ["../escape.json", "windows/escape.json", "content/../../escape.json"])
def test_window_resource_path_is_still_guarded_by_session_reference_api(tmp_path, pointer):
    store = ModelWindowStore(SessionConversationFiles(tmp_path))
    saved = _save(store)
    bad_reference = {**saved.reference, "path": pointer}
    with pytest.raises(ValueError, match="windows|outside controlled|invalid content reference|content reference unavailable"):
        store.load(SID, bad_reference)
