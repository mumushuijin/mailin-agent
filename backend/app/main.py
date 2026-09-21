from contextlib import asynccontextmanager
import asyncio
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import router as api_router
from app.core.exceptions import AppError
from app.agent.graph import get_graph
from app.agent.hooks import register_hooks
from app.core.logging import get_logger, log_scope, new_trace_id, setup_logging
from app.core.settings import get_settings, init_workspace
from app.core.telemetry import setup_telemetry
from app.resilience import register_default_policies
from app.storage.checkpoint import close_checkpointer, init_checkpointer
from app.tools.mcp import shutdown_mcp_servers
from app.tools.registry import clear_tools_cache

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(level=settings.log_level, log_format=settings.log_format)
    init_workspace(settings)
    setup_telemetry()
    register_default_policies()
    register_hooks()
    await init_checkpointer()
    # 不在启动时连接 MCP，避免后台 discover 与事件循环争抢导致 API 无响应
    clear_tools_cache()
    await asyncio.to_thread(get_graph)

    from app.maintenance import get_maintenance_scheduler

    scheduler = get_maintenance_scheduler()
    await scheduler.start()

    yield

    await scheduler.stop()
    await asyncio.to_thread(shutdown_mcp_servers)
    clear_tools_cache()
    get_graph.cache_clear()
    await close_checkpointer()


app = FastAPI(
    title="麦林 Mailin API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-Id") or new_trace_id()
    start = time.monotonic()
    with log_scope(
        trace_id=trace_id,
        http_method=request.method,
        http_path=request.url.path,
    ):
        try:
            response = await call_next(request)
            duration_ms = (time.monotonic() - start) * 1000
            log.info(
                "request.completed",
                status_code=response.status_code,
                duration_ms=round(duration_ms, 1),
            )
            response.headers["X-Trace-Id"] = trace_id
            return response
        except Exception:
            duration_ms = (time.monotonic() - start) * 1000
            log.error(
                "request.failed",
                duration_ms=round(duration_ms, 1),
                exc_info=True,
            )
            raise


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    body: dict = {"detail": exc.message}
    if getattr(exc, "code", None):
        body["code"] = exc.code
    payload = getattr(exc, "payload", None) or {}
    if payload:
        body.update(payload)
    return JSONResponse(status_code=exc.status_code, content=body)


@app.get("/health")
async def health():
    settings = get_settings()
    return {
        "status": "ok",
        "has_llm": settings.has_llm,
    }


app.include_router(api_router, prefix="/api")
