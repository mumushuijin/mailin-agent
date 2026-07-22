import contextlib
import json
import time
import uuid

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from starlette.responses import JSONResponse

from app.core.logging import get_logger, log_scope
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat_service import ChatService

router = APIRouter()
chat_service = ChatService()
log = get_logger(__name__)


@contextlib.contextmanager
def _http_run_scope(session_id: str | None):
    run_id = str(uuid.uuid4())
    start = time.monotonic()
    status = "success"

    with log_scope(run_id=run_id, session_id=session_id):
        log.info("run.started")
        try:
            yield run_id
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


@router.post("/send")
async def send_message(body: ChatRequest):
    with _http_run_scope(body.session_id) as run_id:
        result = await chat_service.send_sync(body.message, body.session_id, run_id=run_id)
        return JSONResponse(content=result.model_dump())


@router.post("/send/sync", response_model=ChatResponse)
async def send_message_sync(body: ChatRequest):
    with _http_run_scope(body.session_id) as run_id:
        return await chat_service.send_sync(body.message, body.session_id, run_id=run_id)


@router.post("/send/stream")
async def send_message_stream(body: ChatRequest):
    run_id = str(uuid.uuid4())

    async def event_generator():
        start = time.monotonic()
        status = "success"

        with log_scope(run_id=run_id, session_id=body.session_id):
            log.info("run.started")
            try:
                async for event in chat_service.send_stream(
                    body.message, body.session_id, run_id=run_id
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
