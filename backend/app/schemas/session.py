from pydantic import BaseModel

PLACEHOLDER_TITLE = "新会话"
TITLE_SOURCE_PLACEHOLDER = "placeholder"
TITLE_SOURCE_AUTO = "auto"
TITLE_SOURCE_USER = "user"


class Session(BaseModel):
    id: str
    created_at: int
    updated_at: int
    workspace_path: str | None = None
    title: str = PLACEHOLDER_TITLE
    title_source: str = TITLE_SOURCE_PLACEHOLDER


class SessionCreate(BaseModel):
    workspace_path: str


class SessionRebind(BaseModel):
    workspace_path: str


class SessionRename(BaseModel):
    title: str


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
    todos: list[dict] | None = None
