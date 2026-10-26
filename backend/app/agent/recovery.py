"""Apply the durable tail to bounded state; uncertain effects stop recovery."""
from __future__ import annotations

import copy
import uuid

from app.agent.state import TERMINAL_TURN_STATUSES, utc_now, validate_agent_state


def is_audit_row(row: dict) -> bool:
    return row.get("message", {}).get("role") == "system" or bool((row.get("extensions") or {}).get("runtime", {}).get("auxiliary"))


def append_window_messages(current: list, incoming: list) -> list:
    """Use application identities, preserving repeated/missing vendor ids."""
    result = list(current)
    positions = {m.additional_kwargs.get("ledger_message_id"): index for index, m in enumerate(result) if getattr(m, "additional_kwargs", {}).get("ledger_message_id")}
    for message in incoming:
        identity = (getattr(message, "additional_kwargs", {}) or {}).get("ledger_message_id")
        if identity and identity in positions:
            result[positions[identity]] = message
        elif any(message is existing for existing in result):
            continue
        else:
            if identity:
                positions[identity] = len(result)
            result.append(message)
    return result


def bounded_model_message(message, row: dict):
    """A checkpoint retains a safe model preview of offloaded tool content.

    The complete, hash-checked body stays in its one ledger resource. A caller
    explicitly resolving a result still receives the complete original body.
    """
    from langchain_core.messages import ToolMessage
    content = row.get("message", {}).get("content")
    if not isinstance(message, ToolMessage) or not isinstance(content, list):
        return message
    references = [block for block in content if isinstance(block, dict) and block.get("type") == "reference"]
    if not references:
        return message
    from app.agent.graph import _safe_tool_summary
    reference = references[0]
    kwargs = dict(message.additional_kwargs or {})
    kwargs["ledger_reference"] = reference
    kwargs["tool_cache_summary"] = _safe_tool_summary(message.content)
    return message.model_copy(update={"content": f"[工具结果已落盘] 资源: {reference['path']} | 摘要: {kwargs['tool_cache_summary']}", "additional_kwargs": kwargs})


def decode_window_row(files, session_id: str, row: dict):
    message = files.decode_row(row, session_id=session_id, resolve_references=True)
    return bounded_model_message(message, row)


def recover_ledger_tail(state: dict, files, *, session_id: str | None = None, stop_incomplete: bool = False,
                        branch_id: str | None = None) -> dict:
    if not state:
        if session_id and files.session_dir(session_id).exists() and any(files.session_dir(session_id).iterdir()):
            events = files.visible_events(session_id, branch_id=branch_id or "main")
            accepted = next((event for event in reversed(events) if event["event_type"] == "message.accepted"), None)
            if not accepted:
                return {}
            from langchain_core.messages import HumanMessage
            from app.agent.state import new_agent_state
            state = new_agent_state(workspace_id=str(files.root.parent), session_id=session_id,
                                    turn_id=accepted["turn_id"], message=HumanMessage(content=""))
            state["context"]["working_message"] = []
            state["ledger_pointer"] = files.ledger_pointer(session_id, 0, branch_id=branch_id or "main")
            # Native graph scheduling is unavailable. Replay facts and stop at a
            # safe domain boundary rather than executing an inferred continuation.
            stop_incomplete = True
        else:
            return {}
    validate_agent_state(state)
    result = copy.deepcopy(state)
    scope = result["scope"]
    session_id = session_id or scope["session_id"]
    turn_id = scope["turn_id"]
    branch_id = branch_id or result.get("ledger_pointer", {}).get("event_cursor", {}).get("branch_id", "main")
    cursor = int(result.get("ledger_pointer", {}).get("applied_seq", 0))
    expected_pointer = files.ledger_pointer(session_id, cursor, branch_id=branch_id)
    if expected_pointer["event_cursor"] != result["ledger_pointer"]["event_cursor"]:
        raise ValueError("checkpoint event cursor identity does not match the durable ledger")
    rows = files.read_tail(session_id, after_seq=cursor, offset=result.get("ledger_pointer", {}).get("applied_offset"), branch_id=branch_id)
    calls = {call["call_id"]: dict(call) for call in result["current_step"].get("tool_calls") or []}
    model_requests = {}
    current_step = result["current_step"]
    current_request = (current_step.get("model") or {}).get("request_id")
    if current_request and current_step.get("step_name") == "agent" and current_step.get("step_status") == "running" and not (current_step.get("model") or {}).get("usage"):
        started = files.message_by_id(session_id, f"msg_model_start_{current_request}")
        settled = files.message_by_id(session_id, f"msg_model_settlement_{current_request}")
        if started and not settled:
            model_requests[current_request] = "running"
    changed = bool(rows)
    for row in rows:
        cursor = row["seq"]
        message = row["message"]
        if row.get("turn_id") != turn_id:
            # Conversation history may span turns; control never crosses turns.
            if not is_audit_row(row):
                result["context"]["working_message"] = append_window_messages(result["context"]["working_message"], [decode_window_row(files, session_id, row)])
            continue
        execution = row.get("execution") or {}
        request_id = (row.get("model") or {}).get("request_id")
        if request_id and execution.get("status") in {"running", "succeeded", "failed", "cancelled", "unknown"}:
            model_requests[request_id] = execution["status"]
        interaction = row.get("interaction") or {}
        if interaction.get("decision") is not None:
            pending = result.get("pending_interaction")
            if pending and pending.get("id") == interaction.get("id"):
                pending["decision"] = interaction["decision"]
            elif result.get("turn_status") not in TERMINAL_TURN_STATUSES:
                result["turn_status"] = "failed"
                result["terminal_reason"] = "interaction_state_incomplete"
        for call in message.get("tool_calls") or []:
            calls.setdefault(call["id"], {"call_id": call["id"], "definition_seq": row["seq"], "status": "pending"})
        call_id = message.get("tool_call_id")
        if not is_audit_row(row):
            result["context"]["working_message"] = append_window_messages(result["context"]["working_message"], [decode_window_row(files, session_id, row)])
            if message["role"] == "tool" and call_id:
                call = calls.setdefault(call_id, {"call_id": call_id})
                call.update({"status": execution.get("status", "failed"), "result_seq": row["seq"]})
                if execution.get("attempt"):
                    call["attempt"] = execution["attempt"]
            continue
        if (row.get("extensions") or {}).get("runtime", {}).get("auxiliary"):
            continue
        subject = next((block.get("subject") for block in message["content"] if isinstance(block, dict) and block.get("type") == "lifecycle"), None) if isinstance(message["content"], list) else None
        runtime = (row.get("extensions") or {}).get("runtime") or {}
        status = execution.get("status")
        if subject == "step" and runtime.get("step_id"):
            if status == "running" and result.get("turn_status") == "created":
                result["turn_status"] = "running"
            if runtime["step_id"] != result["current_step"].get("step_id"):
                result["current_step"] = {"step_id": runtime["step_id"], "step_name": runtime.get("node", "tools"), "step_status": "waiting_user" if status == "waiting" else status, "tool_calls": []}
            else:
                result["current_step"]["step_status"] = "waiting_user" if status == "waiting" else status
            if execution.get("started_at") is not None:
                result["current_step"]["started_at"] = execution["started_at"]
            if execution.get("finished_at") is not None:
                result["current_step"]["ended_at"] = execution["finished_at"]
        elif subject == "tool" and call_id:
            # Evidence received after a final settlement is diagnostic only.
            if any(block.get("detail") == "late_result_evidence" for block in message["content"] if isinstance(block, dict)):
                continue
            call = calls.setdefault(call_id, {"call_id": call_id})
            if not call.get("result_seq"):
                call["status"] = status
                if execution.get("attempt"):
                    call["attempt"] = execution["attempt"]
        elif subject == "model_request" and row.get("model", {}).get("request_id"):
            result["current_step"]["model"] = dict(row["model"])
        elif subject == "interaction":
            interaction = row.get("interaction") or {}
            if status == "waiting":
                result["pending_interaction"] = {**interaction, "call_id": call_id, "definition_seq": row["seq"]}
                result["turn_status"] = "waiting_user"
                result["current_step"]["step_status"] = "waiting_user"
            elif status in {"cancelled", "failed", "unknown"}:
                result["pending_interaction"] = None
                result["turn_status"] = "cancelled" if status == "cancelled" else "failed"
                result["terminal_reason"] = runtime.get("reason") or execution.get("error", {}).get("code") or "interaction_unanswered"
        elif subject == "turn" and status in TERMINAL_TURN_STATUSES:
            result["turn_status"] = status
            result["terminal_reason"] = runtime.get("reason") or execution.get("error", {}).get("code") or status
            result["pending_interaction"] = None
        elif subject == "task" and "task_id" in runtime:
            task_id = runtime["task_id"]
            if task_id and task_id not in result["tasks"]:
                result["tasks"].append(task_id)
            if task_id:
                result.setdefault("task_details", {}).setdefault(task_id, {})["status"] = status
            if any(block.get("detail") == "selected" for block in message["content"] if isinstance(block, dict)):
                result["current_task"] = task_id
        elif subject == "context":
            context = (row.get("extensions") or {}).get("context") or {}
            if context.get("summary_ref"):
                files.read_summary(session_id, context["summary_ref"])
                result["context"]["summary_pointer"] = context["summary_ref"]
                result["context"]["compression_count"] = context.get("compression_count", result["context"].get("compression_count", 0))
            if context.get("window_ref"):
                from app.storage.model_window import ModelWindowStore
                window = ModelWindowStore(files).load(
                    session_id,
                    context["window_ref"],
                    expected_source_event_cursor=context.get("source_event_cursor"),
                    expected_window_schema_version=context.get("window_schema_version"),
                    expected_compression_version=context.get("compression_version"),
                    expected_bootstrap_fingerprint=context.get("bootstrap_fingerprint"),
                    expected_reference_fingerprint=context.get("reference_fingerprint"),
                )
                if window.content_sha256 != context.get("content_sha256"):
                    raise ValueError("context window content hash does not match its ledger event")
                result["context"]["window_pointer"] = window.reference
    visible_events = files.visible_events(session_id, branch_id=branch_id)
    for event in visible_events:
        if event["event_type"] == "memory.sync.recorded":
            result["memory"] = copy.deepcopy(event["payload"])
        elif (event["event_type"] == "context.window.materialized"
              and event["event_seq"] > int(result["ledger_pointer"].get("applied_seq", 0))):
            # visible_events has verified native materializations even without a message projection.
            result["context"]["window_pointer"] = copy.deepcopy(event["payload"]["window_ref"])
    if visible_events and visible_events[-1]["event_seq"] > cursor:
        cursor = visible_events[-1]["event_seq"]
        changed = True
    window_ref = result["context"].get("window_pointer")
    if window_ref:
        import hashlib
        import json
        from app.storage.message_codec import encode_message
        from app.storage.model_window import ModelWindowStore
        references = [block for message in result["context"].get("working_message") or []
                      for block in (encode_message(message).get("message", {}).get("content") or [])
                      if isinstance(block, dict) and block.get("type") == "reference"]
        reference_bytes = json.dumps(references, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        latest_context_event = next((event for event in reversed(visible_events) if event["event_type"] in {
            "message.accepted", "model.response.completed", "tool.attempt.settled",
        }), None)
        source_cursor = {"branch_id": branch_id, "event_seq": latest_context_event["event_seq"] if latest_context_event else 0,
                         "event_id": latest_context_event["event_id"] if latest_context_event else "origin"}
        window = ModelWindowStore(files).load_if_current(
            session_id, window_ref, expected_source_event_cursor=source_cursor,
            expected_window_schema_version="working-window-v1", expected_compression_version="four-layer-v1",
            expected_bootstrap_fingerprint=result["context"].get("bootstrap_fingerprint") or hashlib.sha256(b"").hexdigest(),
            expected_reference_fingerprint=hashlib.sha256(reference_bytes).hexdigest(),
        )
        if window is None:
            result["context"]["window_pointer"] = None
            result["context"]["last_built_at"] = None
            changed = True
    unknown = []
    if result.get("turn_status") not in TERMINAL_TURN_STATUSES:
        from langchain_core.messages import ToolMessage
        from app.agent.audit import RuntimeLedgerAudit
        audit = RuntimeLedgerAudit(files, scope, branch_id=branch_id)
        uncertain = [call for call in calls.values() if call.get("status") == "running" and not call.get("result_seq")]
        changed = changed or bool(uncertain)
        for call in uncertain:
            attempt = int(call.get("attempt") or 1)
            settled = audit.settle_tool(ToolMessage(tool_call_id=call["call_id"], content="工具已准备启动，但无法确认实际执行结果；已停止本轮，禁止自动重复执行。", additional_kwargs={"tool_status": "unknown", "attempt": attempt, "timestamp": utc_now(), "reason_code": "execution_outcome_unknown"}))
            call.update({"status": settled.additional_kwargs["tool_status"], "result_seq": settled.additional_kwargs["ledger_seq"]})
            if call["status"] == "unknown":
                unknown.append(call)
            row = files.read_tail(session_id, after_seq=call["result_seq"] - 1, branch_id=branch_id)[0]
            result["context"]["working_message"] = append_window_messages(result["context"]["working_message"], [bounded_model_message(settled, row)])
            cursor = max(cursor, int(settled.additional_kwargs["ledger_seq"]))
        if unknown:
            result["turn_status"] = "failed"
            result["terminal_reason"] = "execution_outcome_unknown"
            result["pending_interaction"] = None
            result["current_step"]["step_status"] = "failed"
            cursor = RuntimeLedgerAudit(files, scope, branch_id=branch_id).turn_terminal("failed", "execution_outcome_unknown")
            changed = True
        unresolved_models = [request_id for request_id, status in model_requests.items() if status == "running"]
        if unresolved_models and result.get("turn_status") not in TERMINAL_TURN_STATUSES:
            for request_id in unresolved_models:
                audit.model_failed(request_id, "模型请求已开始，但恢复时无法确认响应。", status="unknown")
            result["turn_status"] = "failed"
            result["terminal_reason"] = "model_request_outcome_unknown"
            result["pending_interaction"] = None
            result["current_step"]["step_status"] = "failed"
            cursor = audit.turn_terminal("failed", "model_request_outcome_unknown")
            changed = True
        if stop_incomplete and result.get("turn_status") in {"created", "running"}:
            for call in calls.values():
                if call.get("status") == "pending" and not call.get("result_seq"):
                    settled = audit.settle_tool(ToolMessage(tool_call_id=call["call_id"], content="恢复后停止未完成轮次，工具调用尚未开始。", additional_kwargs={"tool_status": "skipped", "reason_code": "interrupted_before_execution", "timestamp": utc_now()}))
                    call.update({"status": settled.additional_kwargs["tool_status"], "result_seq": settled.additional_kwargs["ledger_seq"]})
                    row = files.message_by_id(session_id, settled.additional_kwargs["ledger_message_id"])
                    result["context"]["working_message"] = append_window_messages(result["context"]["working_message"], [bounded_model_message(settled, row)])
            if result["current_step"].get("step_status") in {"pending", "running"}:
                step = result["current_step"]
                audit.step_id = step.get("step_id")
                audit.node("failed", name=step.get("step_name") or "agent")
                step["step_status"] = "failed"
                step["ended_at"] = utc_now()
            result["turn_status"] = "failed"
            result["terminal_reason"] = "interrupted_turn"
            result["pending_interaction"] = None
            cursor = audit.turn_terminal("failed", "interrupted_turn")
            changed = True
    result["current_step"]["tool_calls"] = list(calls.values()) if result["current_step"].get("step_name") == "tools" or unknown or result.get("terminal_reason") == "interrupted_turn" else result["current_step"].get("tool_calls", [])
    if result.get("turn_status") in TERMINAL_TURN_STATUSES:
        for task_id in result["tasks"]:
            detail = result.setdefault("task_details", {}).get(task_id) or {}
            if detail.get("status") not in TERMINAL_TURN_STATUSES:
                detail["status"] = result["turn_status"]
                result["task_details"][task_id] = detail
    if changed:
        result["ledger_pointer"] = files.ledger_pointer(session_id, cursor, branch_id=branch_id)
        result["state_revision"] = int(result.get("state_revision", 0)) + 1
        result["checkpoint_uid"] = f"cp_{uuid.uuid4().hex}"
        result.pop("checkpoint_id", None)
    return result
