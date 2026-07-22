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
    error: str | None = None
    session_id: str | None = None
    step: int | None = None
    max_steps: int | None = None
