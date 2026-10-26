import contextlib
import json
import time
import uuid

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.core.logging import get_logger, log_scope
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat_service import ChatService

router = APIRouter()
chat_service = ChatService()
log = get_logger(__name__)


@contextlib.contextmanager
def _http_run_scope(session_id: str | None, turn_id: str | None = None):
    turn_id = turn_id or "turn_" + uuid.uuid4().hex
    start = time.monotonic()
    status = "success"

    with log_scope(turn_id=turn_id, session_id=session_id):
        log.info("run.started")
        try:
            yield turn_id
        except Exception as exc:
            status = "failed"
            log.error("run.failed", error=str(exc), exc_info=True)
            raise
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            log.info(
                "run.completed",
                status=status,
                duration_ms=round(duration_ms, 1),
                session_id=session_id,
            )


@router.post("/send", response_model=ChatResponse)
async def send_message(body: ChatRequest):
    with _http_run_scope(body.session_id, body.turn_id) as turn_id:
        return await chat_service.send_sync(body.message, body.session_id, turn_id=turn_id,
                                            submission_id=body.submission_id, branch_id=body.branch_id,
                                            fork_cursor=body.fork_cursor.model_dump() if body.fork_cursor else None)


@router.post("/send/stream")
async def send_message_stream(body: ChatRequest):
    turn_id = body.turn_id or "turn_" + uuid.uuid4().hex

    async def event_generator():
        start = time.monotonic()
        status = "success"

        with log_scope(turn_id=turn_id, session_id=body.session_id):
            log.info("run.started")
            try:
                async for event in chat_service.send_stream(
                    body.message, body.session_id, turn_id=turn_id, submission_id=body.submission_id,
                    branch_id=body.branch_id,
                    fork_cursor=body.fork_cursor.model_dump() if body.fork_cursor else None,
                ):
                    yield event
            except Exception as e:
                status = "failed"
                log.error("run.failed", error=str(e), exc_info=True)
                yield f"event: error\ndata: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
            finally:
                duration_ms = (time.monotonic() - start) * 1000
                log.info(
                    "run.completed",
                    status=status,
                    duration_ms=round(duration_ms, 1),
                    session_id=body.session_id,
                )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
