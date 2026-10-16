"""Durable lifecycle messages written at actual execution boundaries."""
from __future__ import annotations

import uuid
import threading
import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from app.agent.state import utc_now

_runtime_audit: ContextVar[RuntimeLedgerAudit | None] = ContextVar("runtime_ledger_audit", default=None)


class RuntimeLedgerAudit:
    # One durable final result per call, shared by active workers and recovery.
    _settlement_lock = threading.RLock()

    def __init__(self, files, scope: dict, *, step_id: str | None = None, task_id: str | None = None):
        self.files = files
        self.scope = dict(scope)
        self.session_id = str(scope.get("session_id") or "")
        self.turn_id = str(scope.get("turn_id") or "")
        self.step_id = step_id
        self.task_id = task_id
        self.last_request_id: str | None = None
        self.started_attempts: dict[str, int] = {}
        self._lock = threading.RLock()

    def lifecycle(self, subject: str, status: str, *, message_id: str | None = None,
                  call_id: str | None = None, model: dict | None = None,
                  execution: dict | None = None, interaction: dict | None = None,
                  extensions: dict | None = None, detail: str | None = None) -> int:
        block: dict[str, Any] = {"type": "lifecycle", "subject": subject}
        if detail:
            block["detail"] = detail
        message = {"role": "system", "content": [block]}
        if call_id:
            message["tool_call_id"] = call_id
        seq, _ = self.files.append(
            self.session_id, message_id or f"msg_{uuid.uuid4().hex}", message,
            turn_id=self.turn_id or None, timestamp=utc_now(), model=model,
            execution={"status": status, **(execution or {})},
            interaction=interaction, extensions=extensions,
        )
        return seq

    def model_started(self, request_id: str, name: str | None = None) -> int:
        self.last_request_id = request_id
        model = {"request_id": request_id}
        if name:
            model["name"] = name
        return self.lifecycle("model_request", "running", message_id=f"msg_model_start_{request_id}", model=model,
                              execution={"started_at": utc_now()})

    def model_failed(self, request_id: str, error: str, *, status: str = "failed") -> int:
        with self._settlement_lock:
            existing = self.files.message_by_id(self.session_id, f"msg_model_settlement_{request_id}")
            if existing:
                return existing["seq"]
            return self.lifecycle("model_request", status, message_id=f"msg_model_settlement_{request_id}", model={"request_id": request_id},
                                  execution={"finished_at": utc_now(), "error": {"code": "model_request_outcome_unknown" if status == "unknown" else "model_request_failed", "message": safe_error(error)}})

    def tool_started(self, call_id: str, attempt: int) -> int:
        with self._lock:
            seq = self.lifecycle("tool", "running", call_id=call_id,
                                 execution={"attempt": attempt, "started_at": utc_now()})
            self.started_attempts[call_id] = attempt
            return seq

    def tool_retry_failed(self, call_id: str, attempt: int, error: str, code: str = "tool_error") -> int:
        return self.lifecycle("tool", "failed", call_id=call_id,
                              execution={"attempt": attempt, "finished_at": utc_now(), "error": {"code": code, "message": safe_error(error)}})

    def tool_late_evidence(self, call_id: str, attempt: int, content: str, status: str) -> int:
        return self.lifecycle("tool", status, call_id=call_id, detail="late_result_evidence",
                              execution={"attempt": attempt, "finished_at": utc_now()},
                              extensions={"runtime": {"late_result": safe_error(content)}})

    def settle_tool(self, message):
        """Persist a completed call and return its authoritative final message.

        A timeout settlement and a worker completion may race. The first final
        row wins; later reliable information is system audit evidence only.
        """
        from langchain_core.messages import ToolMessage
        if not isinstance(message, ToolMessage) or not message.tool_call_id:
            raise ValueError("tool settlement requires a tool call id")
        identity = "msg_tool_" + hashlib.sha256(f"{self.turn_id}:{message.tool_call_id}".encode("utf-8")).hexdigest()[:32]
        with self._settlement_lock:
            existing = self.files.message_by_id(self.session_id, identity)
            if existing:
                settled = self.files.decode_row(existing, session_id=self.session_id, resolve_references=True)
                if message.additional_kwargs.get("tool_status", "succeeded") != "unknown" and (settled.content != message.content or (existing.get("execution") or {}).get("status") != message.additional_kwargs.get("tool_status", "succeeded")):
                    self.tool_late_evidence(message.tool_call_id,
                                            int(message.additional_kwargs.get("attempt") or self.started_attempts.get(message.tool_call_id) or 1),
                                            str(message.content), message.additional_kwargs.get("tool_status", "succeeded"))
                return settled
            message.additional_kwargs["ledger_message_id"] = identity
            timestamp = message.additional_kwargs.setdefault("timestamp", utc_now())
            execution = {"status": message.additional_kwargs.get("tool_status", "succeeded"), "finished_at": timestamp}
            if message.additional_kwargs.get("attempt"):
                execution["attempt"] = message.additional_kwargs["attempt"]
            if message.additional_kwargs.get("reason_code") not in (None, "ok"):
                execution["error"] = {"code": message.additional_kwargs["reason_code"]}
            message.additional_kwargs["ledger_execution"] = execution
            message.additional_kwargs["ledger_seq"] = self.files.append_messages(self.session_id, self.turn_id, [message], scope=self.scope)
            return message

    def tool_result(self, message) -> int:
        settled = self.settle_tool(message)
        return int(settled.additional_kwargs["ledger_seq"])

    def node(self, status: str, *, name: str, message_id: str | None = None) -> int:
        status = "waiting" if status == "waiting_user" else status
        runtime = {"step_id": self.step_id, "node": name}
        if self.task_id:
            runtime["task_id"] = self.task_id
        return self.lifecycle("step", status, message_id=message_id,
                              extensions={"runtime": runtime},
                              execution={"started_at" if status == "running" else "finished_at": utc_now()})

    def turn_terminal(self, status: str, reason: str) -> int:
        # Repeated transport finalization must reuse an immutable settlement.
        with self._settlement_lock:
            existing = self.files.message_by_id(self.session_id, f"msg_terminal_{self.turn_id}")
            if existing:
                return existing["seq"]
            return self.lifecycle("turn", status,
                                  message_id=f"msg_terminal_{self.turn_id}",
                                  execution={"finished_at": utc_now()},
                                  extensions={"runtime": {"reason": reason}})


def safe_error(value: Any) -> str:
    from app.agent.graph import _safe_tool_summary
    return _safe_tool_summary(value)


def get_runtime_audit() -> RuntimeLedgerAudit | None:
    return _runtime_audit.get()


@contextmanager
def bind_runtime_audit(audit: RuntimeLedgerAudit):
    token = _runtime_audit.set(audit)
    try:
        yield audit
    finally:
        _runtime_audit.reset(token)
