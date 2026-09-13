from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    content: str
    session_id: str | None = None


class StreamEventPayload(BaseModel):
    type: str
    content: str | None = None
    tool: str | None = None
    args: dict | None = None
    result: str | None = None
    message: str | None = None
    error: str | None = None
    session_id: str | None = None
    step: int | None = None
    max_steps: int | None = None
    run_id: str | None = None
    stage: str | None = None
    status: str | None = None
    elapsed_ms: float | None = None
    duration_ms: float | None = None
    metrics: dict | None = None
    tool_call_id: str | None = None
    kind: str | None = None
    prompt: str | None = None
    reason: str | None = None
    options: list[str] | None = None
    allow_multiple: bool | None = None
    todos: list[dict] | None = None
    background: dict | None = None
    config_name: str | None = None
    context_usage: dict | None = None
    api_usage: dict | None = None
    session_token_stats: dict | None = None
    compression: dict | None = None
    partial: bool | None = None
    cancelled: bool | None = None
