import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.graph import make_thread_config
from app.services.session_service import SessionService
from app.storage.conversation_files import SessionConversationFiles


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["delete", "clear_all"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_session_deletion_cleans_known_branch_checkpoint_threads(tmp_path, monkeypatch, operation, legacy):
    sid = "00000000-0000-4000-8000-000000000001"
    files = SessionConversationFiles(tmp_path)
    files.fork_branch(sid, "child", parent_cursor={"branch_id": "main", "event_seq": 0, "event_id": "origin"})
    fork = files.read_events(sid)[0]
    files.fork_branch(sid, "grandchild", parent_cursor={key: fork[key] for key in ("branch_id", "event_seq", "event_id")})
    if legacy:
        path = files.metadata_path(sid)
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta["schema_version"] = 2
        path.write_text(json.dumps(meta), encoding="utf-8")
    service = SessionService.__new__(SessionService)
    service.conversation_files = files
    service.store = SimpleNamespace(delete=MagicMock(), clear_all=MagicMock(), list=lambda: [{"id": sid}])
    deleted = AsyncMock()
    reset = AsyncMock()
    monkeypatch.setattr("app.services.session_service.delete_thread", deleted)
    monkeypatch.setattr("app.services.session_service.reset_checkpointer", reset)

    if operation == "delete":
        await service.delete(sid)
        service.store.delete.assert_called_once_with(sid)
        reset.assert_not_awaited()
    else:
        await service.clear_all()
        service.store.clear_all.assert_called_once_with()
        reset.assert_awaited_once_with()

    branches = ["main"] if legacy else ["main", "child", "grandchild"]
    assert [call.args[0] for call in deleted.await_args_list] == [
        make_thread_config(sid, branch)["configurable"]["thread_id"] for branch in branches
    ]
    assert not files.session_dir(sid).exists()
