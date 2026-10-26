from fastapi import APIRouter, Query
from fastapi.responses import Response
from app.core.exceptions import AppError

from app.schemas.session import (
    Session,
    SessionCreate,
    SessionHistory,
    SessionHistoryPage,
    SessionRebind,
    SessionRename,
    ToolResultPayload,
)
from app.services.chat_service import ChatService
from app.services.session_service import SessionService
from app.services.timeline_service import TimelineService

router = APIRouter()
session_service = SessionService()
chat_service = ChatService()
timeline_service = TimelineService(session_service.conversation_files)


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


@router.get("/{session_id}/history", response_model=SessionHistory | SessionHistoryPage,
            response_model_exclude_none=True, response_model_exclude_defaults=True)
async def get_session_history(
    session_id: str,
    limit: int | None = Query(default=None, ge=1, le=200),
    before: str | None = Query(default=None),
    full: bool = Query(default=False),
):
    try:
        if full or (limit is None and before is None):
            return await chat_service.get_history(session_id)
        return await chat_service.get_history_page(
            session_id, limit=limit or 30, before=before,
        )
    except (ValueError, RuntimeError) as exc:
        raise AppError(str(exc), status_code=409, code="unsupported_session_format") from exc


@router.get("/{session_id}/timeline")
async def get_session_timeline(
    session_id: str,
    branch_id: str = Query(default="main", min_length=1, max_length=256),
    limit: int = Query(default=100, ge=1, le=200),
    after_cursor: str | None = Query(default=None, max_length=2048),
):
    try:
        session_service.get(session_id)
        return timeline_service.read_page(
            session_id,
            branch_id=branch_id,
            limit=limit,
            after_cursor=after_cursor,
        )
    except ValueError as exc:
        raise AppError(str(exc), status_code=409, code="invalid_timeline_cursor") from exc


@router.get("/{session_id}/events")
async def get_session_events(
    session_id: str,
    branch_id: str = Query(default="main", min_length=1, max_length=256),
    after_cursor: str | None = Query(default=None, max_length=2048),
    limit: int = Query(default=100, ge=1, le=200),
):
    try:
        session_service.get(session_id)
        return timeline_service.read_page(
            session_id,
            branch_id=branch_id,
            limit=limit,
            after_cursor=after_cursor,
            include_projection=False,
        )
    except ValueError as exc:
        raise AppError(str(exc), status_code=409, code="invalid_timeline_cursor") from exc


@router.get("/{session_id}/tool-result/{tool_call_id}", response_model=ToolResultPayload)
async def get_tool_result(session_id: str, tool_call_id: str, seq: int | None = Query(default=None, ge=1)):
    if seq is None:
        return await chat_service.get_tool_result(session_id, tool_call_id)
    return await chat_service.get_tool_result(session_id, tool_call_id, seq=seq)


@router.get("/{session_id}/ledger-message/{seq}")
async def get_ledger_message(session_id: str, seq: int):
    try:
        return await chat_service.get_ledger_message(session_id, seq)
    except ValueError as exc:
        raise AppError(str(exc), status_code=409, code="invalid_ledger_resource") from exc


@router.get("/{session_id}/diagnostics")
async def export_session_diagnostics(session_id: str):
    try:
        content = session_service.export_diagnostics(session_id)
    except ValueError as exc:
        raise AppError(str(exc), status_code=409, code="invalid_ledger_resource") from exc
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="session-diagnostics.zip"'},
    )
