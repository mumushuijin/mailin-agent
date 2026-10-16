from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.schemas.chat import ChatRequest
from app.agent.streaming.events import AgentEvent, to_sse, to_ws
from app.services.ws_chat_service import WsChatService
from app.services.chat_service import ChatService, _session_execution
from app.storage.conversation_files import SessionConversationFiles
from app.core.exceptions import AppError


def test_http_rejects_old_application_run_field():
    with pytest.raises(ValidationError):
        ChatRequest(message="hello", run_id="legacy")
    assert ChatRequest(message="hello", turn_id="turn_1").turn_id == "turn_1"


def test_transports_preserve_turn_and_original_model_id():
    event = AgentEvent("chunk", {"content": "hello", "model_message_id": "run--original"}, turn_id="turn_1")
    ws = to_ws(event)
    assert ws["turn_id"] == "turn_1"
    assert ws["data"]["model_message_id"] == "run--original"
    assert "run_id" not in ws
    assert '"turn_id": "turn_1"' in to_sse(event)


@pytest.mark.asyncio
async def test_ws_rejects_old_run_without_starting_work():
    manager = SimpleNamespace(send_event=AsyncMock())
    service = WsChatService(manager, chat_service=SimpleNamespace())
    service._start_run = AsyncMock()
    await service.handle_message(1, {"op": "chat.send", "run_id": "legacy", "message": "hello"})
    service._start_run.assert_not_awaited()
    assert manager.send_event.await_args.args[1].data["reason_code"] == "unsupported_protocol"


@pytest.mark.asyncio
async def test_cancel_requires_matching_session_and_turn():
    service = WsChatService(SimpleNamespace(send_event=AsyncMock()), chat_service=SimpleNamespace())
    service._run_sessions["turn_1"] = "session_1"
    service.cancel_run = AsyncMock()
    await service.handle_message(1, {"op": "chat.cancel", "turn_id": "turn_1", "session_id": "session_2"})
    service.cancel_run.assert_not_awaited()
    await service.handle_message(1, {"op": "chat.cancel", "turn_id": "turn_1", "session_id": "session_1"})
    service.cancel_run.assert_awaited_once_with("turn_1")


def test_repeated_turn_cannot_repeat_model_or_tool_work(tmp_path):
    service = ChatService.__new__(ChatService)
    service.settings = SimpleNamespace(workspace_path=tmp_path)
    service.conversation_files = SessionConversationFiles(tmp_path)
    service._bootstrap_fingerprint = lambda session_id: "test"
    sid = "00000000-0000-4000-8000-000000000001"
    state = service._new_run_state(sid, "hello", 4, "turn_1")
    assert service.conversation_files.latest_seq(sid) == 1
    with pytest.raises(AppError, match="不能重复执行"):
        service._new_run_state(sid, "hello", 4, "turn_1")
    assert service.conversation_files.latest_seq(sid) == 1
    state["turn_status"] = "waiting_user"
    with pytest.raises(AppError, match="尚未结束"):
        service._new_run_state(sid, "another", 4, "turn_2", state)


def test_session_execution_is_shared_across_transports_and_released(tmp_path):
    with _session_execution(tmp_path, "session_1"):
        with pytest.raises(AppError, match="进行中的 turn"):
            with _session_execution(tmp_path, "session_1"):
                pytest.fail("second channel entered")
        with _session_execution(tmp_path, "session_2"):
            pass
    with _session_execution(tmp_path, "session_1"):
        pass
