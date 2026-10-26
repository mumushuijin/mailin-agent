"""Window-only resources survive diagnostics export and retain read integrity gates."""
from __future__ import annotations

import hashlib
import json

import pytest
from langchain_core.messages import SystemMessage

from app.agent.audit import RuntimeLedgerAudit
from app.storage.conversation_files import SessionConversationFiles
from app.storage.model_window import ModelWindowStore

SESSION = "00000000-0000-4000-8000-000000000071"
TURN = "turn_window_resources"


def _materialized_window(files, *, native):
    resource = files.write_content(SESSION, "content/window-only/source.txt", "only the model window references this")
    fingerprints = hashlib.sha256(json.dumps([resource], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    window = ModelWindowStore(files).save(
        SESSION, [SystemMessage(content=[resource])],
        source_event_cursor={"branch_id": "main", "event_seq": 0, "event_id": "origin"},
        window_schema_version="working-window-v1", compression_version="four-layer-v1",
        bootstrap_fingerprint=hashlib.sha256(b"").hexdigest(), reference_fingerprint=fingerprints,
    )
    payload = {
        "window_ref": window.reference, "window_schema_version": window.window_schema_version,
        "compression_version": window.compression_version, "content_sha256": window.content_sha256,
        "source_event_cursor": window.source_event_cursor, "bootstrap_fingerprint": window.bootstrap_fingerprint,
        "reference_fingerprint": window.reference_fingerprint,
    }
    if native:
        seq, _ = files.append_event(SESSION, "context.window.materialized", payload, event_id="evt_native_window", turn_id=TURN)
    else:
        scope = {"workspace_id": files.workspace_id, "session_id": SESSION, "turn_id": TURN}
        seq = RuntimeLedgerAudit(files, scope).lifecycle("context", "completed", extensions={"context": payload})
    return resource, window, payload, seq


@pytest.mark.parametrize("native", [False, True])
def test_export_contains_and_can_restore_window_only_dependencies(tmp_path, native):
    files = SessionConversationFiles(tmp_path / "source")
    resource, window, payload, seq = _materialized_window(files, native=native)
    if native:
        assert files.read(SESSION) == []
        assert files.append_event(SESSION, "context.window.materialized", payload, event_id="evt_native_window", turn_id=TURN) == (seq, False)

    exported = files.export_files(SESSION)

    assert exported[resource["path"]] == files.read_reference(SESSION, resource)
    assert exported[window.reference["path"]] == files.read_reference(SESSION, window.reference)
    restored = SessionConversationFiles(tmp_path / "restored", workspace_id=files.workspace_id)
    for name, data in exported.items():
        if name.startswith(("content/", "tool-results/")):
            restored.write_content(SESSION, name, data)
            continue
        destination = restored.session_dir(SESSION) / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    restored_window = ModelWindowStore(restored).load(SESSION, window.reference)
    assert restored_window.messages == window.messages
    assert restored.read_reference(SESSION, resource) == files.read_reference(SESSION, resource)
    assert restored.read_events(SESSION) == files.read_events(SESSION)


@pytest.mark.parametrize("target", ["window", "dependency"])
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_native_window_reads_retries_and_export_reject_damaged_resources(tmp_path, target, damage):
    files = SessionConversationFiles(tmp_path)
    resource, window, payload, seq = _materialized_window(files, native=True)
    reference = window.reference if target == "window" else resource
    path = files.session_dir(SESSION) / reference["path"]
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupt resource")
    ledger = files.ledger_path(SESSION).read_bytes()
    # Use a new index too: no cached projection may hide a damaged native event.
    reader = SessionConversationFiles(tmp_path)
    operations = [
        lambda: reader.read_events(SESSION),
        lambda: reader.read(SESSION),
        lambda: reader.read_tail(SESSION),
        lambda: reader.row_at(SESSION, seq),
        lambda: reader.export_files(SESSION),
        lambda: reader.append_event(SESSION, "context.window.materialized", payload, event_id="evt_native_window", turn_id=TURN),
    ]
    for operation in operations:
        with pytest.raises(ValueError, match="unavailable|integrity"):
            operation()
    assert files.ledger_path(SESSION).read_bytes() == ledger


@pytest.mark.parametrize("field", [
    "content_sha256", "bootstrap_fingerprint", "reference_fingerprint",
    "window_schema_version", "compression_version", "source_event_cursor", "window_ref",
])
def test_native_window_provenance_is_revalidated_on_read_and_idempotent_retry(tmp_path, field):
    files = SessionConversationFiles(tmp_path)
    _, _, _, _ = _materialized_window(files, native=True)
    event = files.read_events(SESSION)[0]
    payload = event["payload"]
    if field == "source_event_cursor":
        payload[field] = {"branch_id": "main", "event_seq": 1, "event_id": "evt_native_window"}
    elif field == "window_ref":
        payload[field]["path"] = "content/windows/../outside.json"
    elif field.endswith("version"):
        payload[field] = "unsupported-v2"
    else:
        payload[field] = "0" * 64
    ledger = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    files.ledger_path(SESSION).write_bytes(ledger)
    reader = SessionConversationFiles(tmp_path)

    with pytest.raises(ValueError, match="stale|content hash|windows"):
        reader.read_events(SESSION)
    with pytest.raises(ValueError, match="stale|content hash|windows"):
        reader.append_event(SESSION, "context.window.materialized", payload, event_id="evt_native_window", turn_id=TURN)

    assert files.ledger_path(SESSION).read_bytes() == ledger
