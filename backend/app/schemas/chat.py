from pydantic import BaseModel, ConfigDict, Field, model_validator


class BranchEventCursor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    branch_id: str = Field(min_length=1, max_length=256)
    event_seq: int = Field(ge=0, strict=True)
    event_id: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_origin(self):
        if (self.event_seq == 0) != (self.event_id == "origin"):
            raise ValueError("origin cursor requires event_seq 0 and event_id origin")
        return self


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str
    session_id: str | None = None
    turn_id: str | None = Field(default=None, min_length=1)
    submission_id: str = Field(min_length=1, max_length=256)
    branch_id: str = Field(default="main", min_length=1, max_length=256)
    fork_cursor: BranchEventCursor | None = None

    @model_validator(mode="after")
    def validate_fork_target(self):
        if self.fork_cursor and self.branch_id in {"main", self.fork_cursor.branch_id}:
            raise ValueError("fork_cursor requires a new child branch_id")
        return self


class ChatResponse(BaseModel):
    content: str
    session_id: str | None = None
    turn_id: str
    branch_id: str = "main"


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
    turn_id: str | None = None
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
