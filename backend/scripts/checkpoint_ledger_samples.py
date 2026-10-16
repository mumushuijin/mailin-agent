"""Reproduce old/new structure sizes using the pinned baseline and current codec."""
import copy
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, message_to_dict
from app.agent.state import new_agent_state
from app.storage.message_codec import encode_message
from app.storage.ledger_contract import validate_row

OUT = ROOT / "openspec/changes/optimize-checkpoint-ledger-fields/samples"
OUT.mkdir(parents=True, exist_ok=True)
BASELINE_COMMIT = "23d3ab1325542c0b9bd94f5a75c9a5cbaeaf9d70"
original = subprocess.check_output(["git", "show", f"{BASELINE_COMMIT}:backend/app/agent/state.py"], cwd=ROOT).decode("utf-8")
old = {}
exec(compile(original, f"{BASELINE_COMMIT}:state.py", "exec"), old)
NOW = 1791986400000
SID = "00000000-0000-4000-8000-000000000001"

def wire(value):
    if hasattr(value, "type") and hasattr(value, "content"):
        return message_to_dict(value)
    raise TypeError(type(value))

def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=wire).encode("utf-8")

def empty_fields(value):
    if isinstance(value, dict):
        return sum(v is None or v == "" or v == [] or v == {} for v in value.values()) + sum(empty_fields(v) for v in value.values())
    return sum(empty_fields(v) for v in value) if isinstance(value, list) else 0

stats = []
for name, tools in (("greeting", False), ("tool-chain", True)):
    messages = [HumanMessage(content="你好" if not tools else "读取文件并给出结论")]
    if tools:
        messages += [AIMessage(content="", id="run--original-provider-id", tool_calls=[{"id": "call_sample", "name": "read_file", "args": {"path": "notes.txt"}}]), ToolMessage(content="文件正文", tool_call_id="call_sample")]
    messages += [AIMessage(content="你好！" if not tools else "文件包含三项待办。", id="run--original-provider-id")]
    prior = old["new_agent_state"](workspace_id="sample-workspace", session_id=SID, message=messages[0], run_id="run_sample")
    fresh = new_agent_state(workspace_id="sample-workspace", session_id=SID, message=copy.deepcopy(messages[0]), turn_id="turn_sample")
    # Compare equivalent initialization, with deterministic UUID lengths.
    prior["context"]["last_built_at"] = "2026-10-14T14:00:00Z"
    fresh["context"]["last_built_at"] = NOW
    older, newer = [], []
    for seq, source in enumerate(messages, 1):
        msg = copy.deepcopy(source)
        msg.additional_kwargs.update(ledger_message_id=f"msg_{seq}", timestamp=NOW)
        if isinstance(msg, AIMessage):
            msg.additional_kwargs["request_id"] = f"req_sample_{seq}"
            msg.response_metadata["finish_reason"] = "tool_calls" if msg.tool_calls else "stop"
        older.append({"seq": seq, "message_id": f"msg_{seq}", "scope": prior["scope"], "origin": {"human": "user", "ai": "assistant", "tool": "tool"}[msg.type], "message": message_to_dict(msg)})
        newer.append({"seq": seq, "message_id": f"msg_{seq}", "turn_id": "turn_sample", "timestamp": NOW, **encode_message(msg)})
    # Necessary process facts are additional rows, counted separately below.
    audit = []
    for subject, status in ([('step', 'running'), ('model_request', 'running'), ('step', 'completed')] + ([('step', 'running'), ('tool', 'running'), ('step', 'completed'), ('step', 'running'), ('model_request', 'running'), ('step', 'completed')] if tools else []) + [('turn', 'completed')]):
        row = {"seq": len(newer) + len(audit) + 1, "message_id": f"msg_audit_{len(audit) + 1}", "timestamp": NOW, "turn_id": "turn_sample", "message": {"role": "system", "content": [{"type": "lifecycle", "subject": subject}]}, "execution": {"status": status}}
        if subject == 'model_request':
            assistant_rows = [r for r in newer if r['message']['role'] == 'assistant']
            request_index = len([r for r in audit if r.get('model')])
            row['model'] = {'request_id': assistant_rows[request_index]['model']['request_id']}
        if subject == 'tool':
            row['message']['tool_call_id'] = 'call_sample'
            row['execution']['attempt'] = 1
        if subject == 'step':
            row['extensions'] = {'runtime': {'step_id': f'step_{len(audit)}', 'node': 'agent'}}
        audit.append(row)
    for row in newer + audit:
        validate_row(row)
    meta = {"schema_version": 2, "workspace_id": fresh["scope"]["workspace_id"], "session_id": SID}
    payload = {"old_checkpoint": prior, "new_checkpoint": fresh, "old_dialogue_rows": older, "new_dialogue_rows": newer, "additional_required_audit_rows": audit, "ledger_meta": meta}
    (OUT / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=wire) + "\n", encoding="utf-8")
    stats.append({"sample": name, "old_checkpoint_bytes": len(compact(prior)), "new_checkpoint_bytes": len(compact(fresh)), "old_dialogue_bytes": sum(len(compact(r)) + 1 for r in older), "new_dialogue_bytes": sum(len(compact(r)) + 1 for r in newer), "new_meta_bytes": len(compact(meta)), "necessary_audit_bytes": sum(len(compact(r)) + 1 for r in audit), "old_rows": len(older), "new_rows": len(newer) + len(audit), "old_default_empty_fields": empty_fields(older), "new_default_empty_fields": empty_fields(newer), "old_container_copies": len(older), "new_container_copies": 1})
(OUT / "sizes.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(stats, ensure_ascii=False, indent=2))
