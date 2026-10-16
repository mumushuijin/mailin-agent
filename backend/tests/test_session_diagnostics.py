import io
import json
import zipfile
from types import SimpleNamespace

from app.services.session_service import SessionService
from app.storage.conversation_files import SessionConversationFiles


def test_diagnostics_bundle_has_one_container_and_complete_resources(tmp_path):
    session = "00000000-0000-4000-8000-000000000001"
    files = SessionConversationFiles(tmp_path)
    files.ensure_metadata(session)
    reference = files.write_content(session, "content/tool-results/result.txt", "full result", media_type="text/plain")
    files.append(session, "msg_result", {"role": "tool", "tool_call_id": "call_1", "content": [reference]},
                 turn_id="turn_1", execution={"status": "succeeded", "attempt": 1})
    summary = files.write_summary(session, "summary_1", {"content": "summary", "covered_seq": 1})
    service = SessionService.__new__(SessionService)
    service.store = SimpleNamespace(get=lambda value: {"id": value})
    service.conversation_files = files
    with zipfile.ZipFile(io.BytesIO(service.export_diagnostics(session))) as bundle:
        names = bundle.namelist()
        assert names.count("ledger_meta.json") == 1
        assert {"ledger.jsonl", reference["path"], summary}.issubset(names)
        assert bundle.read(reference["path"]) == b"full result"
        row = json.loads(bundle.read("ledger.jsonl").splitlines()[0])
        assert "workspace_id" not in row and "session_id" not in row
