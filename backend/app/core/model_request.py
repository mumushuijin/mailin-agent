"""Assign a distinct identity to each actual auxiliary model request."""
from __future__ import annotations

import uuid
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar

from langchain_core.messages import AIMessage


_active_scope: ContextVar[dict | None] = ContextVar("model_request_scope", default=None)


@contextmanager
def bind_model_request_scope(scope: dict):
    from app.storage.ledger_contract import validate_scope
    validate_scope(scope)
    token = _active_scope.set(dict(scope))
    try:
        yield
    finally:
        _active_scope.reset(token)


def _start_request(model):
    from app.agent.audit import get_runtime_audit
    request_id = f"req_{uuid.uuid4().hex}"
    scope = _active_scope.get() or {}
    metadata = {**scope, "request_id": request_id}
    audit = get_runtime_audit()
    if audit:
        audit.model_started(request_id, str(getattr(model, "model_name", "") or "") or None)
    return request_id, metadata, audit


def _settle_response(response, request_id, audit):
    if isinstance(response, AIMessage):
        response.additional_kwargs["request_id"] = request_id
        if audit:
            from app.agent.state import utc_now
            response.additional_kwargs["timestamp"] = utc_now()
            response.additional_kwargs["ledger_extensions"] = {"runtime": {"auxiliary": True}}
            audit.files.append_messages(
                audit.session_id, audit.turn_id, [response], scope=audit.scope, branch_id=audit.branch_id,
            )
    return response


def invoke_auxiliary_model(model, messages):
    request_id, metadata, audit = _start_request(model)
    try:
        response = model.invoke(messages, config={"metadata": metadata})
    except BaseException as exc:
        if audit:
            audit.model_failed(request_id, str(exc), status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed")
        raise
    return _settle_response(response, request_id, audit)


async def invoke_auxiliary_model_async(model, messages):
    """Audit one actual async attempt, retaining the provider's message id."""
    request_id, metadata, audit = _start_request(model)
    try:
        response = await model.ainvoke(messages, config={"metadata": metadata})
    except BaseException as exc:
        if audit:
            audit.model_failed(request_id, str(exc), status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed")
        raise
    return _settle_response(response, request_id, audit)
