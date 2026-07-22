from fastapi import APIRouter

from app.api import chat, config, memory, session, ws

router = APIRouter()
router.include_router(chat.router, prefix="/chat", tags=["chat"])
router.include_router(session.router, prefix="/session", tags=["session"])
router.include_router(memory.router, prefix="/memory", tags=["memory"])
router.include_router(config.router, prefix="/config", tags=["config"])
router.include_router(ws.router, prefix="/ws", tags=["websocket"])
