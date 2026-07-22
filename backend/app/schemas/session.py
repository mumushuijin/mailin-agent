from pydantic import BaseModel


class Session(BaseModel):
    id: str
    created_at: int
    updated_at: int


class ToolCallFunction(BaseModel):
    name: str
    arguments: str


class ToolCall(BaseModel):
    id: str
    type: str = "function"
    function: ToolCallFunction


class ChatMessage(BaseModel):
    role: str
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    timestamp: int | None = None


class SessionHistory(BaseModel):
    session_id: str
    messages: list[ChatMessage]
    context_usage: dict | None = None
    api_usage: dict | None = None
    session_token_stats: dict | None = None
