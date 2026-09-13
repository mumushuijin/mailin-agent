from fastapi import APIRouter

from app.schemas.session import Session, SessionCreate, SessionHistory, SessionRebind, SessionRename
from app.services.chat_service import ChatService
from app.services.session_service import SessionService

router = APIRouter()
session_service = SessionService()
chat_service = ChatService()


@router.get("/list")
async def list_sessions() -> dict:
    sessions = session_service.list_sessions()
    return {"sessions": [s.model_dump() for s in sessions]}


@router.post("/create")
async def create_session(body: SessionCreate) -> dict:
    session_id = session_service.create(body.workspace_path)
    return {"session_id": session_id}


@router.post("/{session_id}/workspace", response_model=Session)
async def rebind_session(session_id: str, body: SessionRebind):
    return session_service.rebind(session_id, body.workspace_path)


@router.patch("/{session_id}/title", response_model=Session)
async def rename_session(session_id: str, body: SessionRename):
    return session_service.rename(session_id, body.title)


@router.get("/{session_id}", response_model=Session)
async def get_session(session_id: str):
    return session_service.get(session_id)


@router.delete("/{session_id}")
async def delete_session(session_id: str):
    await session_service.delete(session_id)
    return {"status": "ok"}


@router.get("/{session_id}/history", response_model=SessionHistory)
async def get_session_history(session_id: str):
    return await chat_service.get_history(session_id)
