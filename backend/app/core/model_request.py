"""Assign a distinct identity to each actual auxiliary model request."""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from langchain_core.messages import AIMessage


_active_scope: ContextVar[dict | None] = ContextVar("model_request_scope", default=None)


@contextmanager
def bind_model_request_scope(scope: dict):
    token = _active_scope.set(dict(scope))
    try:
        yield
    finally:
        _active_scope.reset(token)


def invoke_auxiliary_model(model, messages):
    request_id = f"req_{uuid.uuid4().hex}"
    scope = _active_scope.get() or {}
    metadata = {**scope, "request_id": request_id}
    response = model.invoke(messages, config={"metadata": metadata})
    if isinstance(response, AIMessage):
        response.additional_kwargs["request_id"] = request_id
    return response
