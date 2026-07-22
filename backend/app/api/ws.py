import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.agent.streaming.events import AgentEvent
from app.core.logging import get_logger, log_scope
from app.services.chat_service import ChatService
from app.services.ws_chat_service import WsChatService
from app.services.ws_manager import get_ws_manager

log = get_logger(__name__)

router = APIRouter()
_chat_service = ChatService()
_ws_service = WsChatService(get_ws_manager(), _chat_service)


@router.websocket("/chat")
async def chat_websocket(websocket: WebSocket):
    manager = get_ws_manager()
    conn_id = await manager.connect(websocket)
    with log_scope(conn_id=conn_id):
        log.info("ws.connected")
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    await manager.send_event(conn_id, AgentEvent("error", {"error": "无效的 JSON"}))
                    continue
                await _ws_service.handle_message(conn_id, payload)
        except WebSocketDisconnect:
            log.info("ws.disconnected", reason="client")
        except Exception:
            log.error("ws.error", exc_info=True)
        finally:
            manager.disconnect(conn_id)
