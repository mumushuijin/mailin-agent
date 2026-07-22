from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from app.agent.streaming.events import AgentEvent, to_ws

logger = logging.getLogger(__name__)


class ConnectionManager:
    """WebSocket 连接池：一客户端一连接，支持会话订阅与广播。"""

    def __init__(self) -> None:
        self._connections: dict[int, WebSocket] = {}
        self._session_subs: dict[str, set[int]] = {}
        self._conn_sessions: dict[int, str | None] = {}

    async def connect(self, websocket: WebSocket) -> int:
        await websocket.accept()
        conn_id = id(websocket)
        self._connections[conn_id] = websocket
        self._conn_sessions[conn_id] = None
        return conn_id

    def disconnect(self, conn_id: int) -> None:
        session_id = self._conn_sessions.pop(conn_id, None)
        self._connections.pop(conn_id, None)
        if session_id and session_id in self._session_subs:
            self._session_subs[session_id].discard(conn_id)
            if not self._session_subs[session_id]:
                del self._session_subs[session_id]

    def subscribe_session(self, conn_id: int, session_id: str | None) -> None:
        old = self._conn_sessions.get(conn_id)
        if old and old in self._session_subs:
            self._session_subs[old].discard(conn_id)
            if not self._session_subs[old]:
                del self._session_subs[old]
        self._conn_sessions[conn_id] = session_id
        if session_id:
            self._session_subs.setdefault(session_id, set()).add(conn_id)

    async def send_event(self, conn_id: int, event: AgentEvent) -> None:
        ws = self._connections.get(conn_id)
        if not ws or ws.client_state != WebSocketState.CONNECTED:
            return
        try:
            await ws.send_json(to_ws(event))
        except Exception:
            logger.debug("WS send failed for conn %s", conn_id, exc_info=True)

    async def broadcast_session(self, session_id: str, event: AgentEvent) -> None:
        for conn_id in list(self._session_subs.get(session_id, ())):
            await self.send_event(conn_id, event)

    async def broadcast_all(self, event: AgentEvent) -> None:
        for conn_id in list(self._connections):
            await self.send_event(conn_id, event)

    def connection_count(self) -> int:
        return len(self._connections)


_manager: ConnectionManager | None = None


def get_ws_manager() -> ConnectionManager:
    global _manager
    if _manager is None:
        _manager = ConnectionManager()
    return _manager
