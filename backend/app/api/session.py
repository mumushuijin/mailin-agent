from fastapi import APIRouter

from app.schemas.session import Session, SessionHistory
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
async def create_session() -> dict:
    session_id = session_service.create()
    return {"session_id": session_id}


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
