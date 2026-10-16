"""Keep the public waiting snapshot durable without consuming graph interrupts."""
from __future__ import annotations

import copy
import uuid

from langgraph.checkpoint.base import BaseCheckpointSaver


class RuntimeCheckpointSaver(BaseCheckpointSaver):
    """Decorate public saver APIs while retaining native checkpoint/task IDs.

    Updating graph state after an interrupt schedules a new task. Persist the
    application waiting channels when the saver receives the native interrupt
    write instead; the graph's pending writes remain attached to the same task.
    """

    def __init__(self, saver, files):
        super().__init__(serde=saver.serde)
        self.saver = saver
        self.files = files

    @property
    def config_specs(self):
        return self.saver.config_specs

    def get_next_version(self, current, channel):
        return self.saver.get_next_version(current, channel)

    def get_tuple(self, config):
        return self.saver.get_tuple(config)

    async def aget_tuple(self, config):
        return await self.saver.aget_tuple(config)

    def list(self, config, *, filter=None, before=None, limit=None):
        yield from self.saver.list(config, filter=filter, before=before, limit=limit)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        async for value in self.saver.alist(config, filter=filter, before=before, limit=limit):
            yield value

    def put(self, config, checkpoint, metadata, new_versions):
        return self.saver.put(config, checkpoint, metadata, new_versions)

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await self.saver.aput(config, checkpoint, metadata, new_versions)

    def _waiting_payload(self, saved, writes):
        from langgraph.types import Interrupt
        if not saved:
            return None
        interrupt_value = None
        for _channel, value in writes:
            interrupts = value if isinstance(value, (tuple, list)) else [value]
            if interrupts and isinstance(interrupts[0], Interrupt):
                interrupt_value = interrupts[0].value
                break
        if not isinstance(interrupt_value, dict) or not interrupt_value.get("interaction_id"):
            return None
        checkpoint = copy.deepcopy(saved.checkpoint)
        channels = checkpoint["channel_values"]
        if channels.get("schema_version") != 2:
            raise ValueError("unsupported runtime checkpoint schema")
        scope = channels["scope"]
        seq = self.files.latest_seq(scope["session_id"])
        current = channels.get("current_step") or {}
        channels["current_step"] = {
            "step_id": interrupt_value["step_id"], "step_name": "tools",
            "step_status": "waiting_user", "tool_calls": current.get("tool_calls") or [],
        }
        for row in self.files.read_tail(scope["session_id"], after_seq=int((channels.get("ledger_pointer") or {}).get("applied_seq", 0))):
            runtime = (row.get("extensions") or {}).get("runtime") or {}
            if runtime.get("step_id") == interrupt_value["step_id"] and (row.get("execution") or {}).get("started_at") is not None:
                channels["current_step"]["started_at"] = row["execution"]["started_at"]
        channels["pending_interaction"] = {
            "id": interrupt_value["interaction_id"], "kind": interrupt_value["kind"],
            "call_id": interrupt_value["tool_call_id"], "definition_seq": interrupt_value["definition_seq"],
            **({"mode": interrupt_value["mode"]} if interrupt_value.get("mode") else {}),
        }
        channels["turn_status"] = "waiting_user"
        channels["ledger_pointer"] = {"applied_seq": seq, "applied_offset": self.files.offset_after(scope["session_id"], seq)}
        channels["state_revision"] = int(channels.get("state_revision", 0)) + 1
        channels["checkpoint_uid"] = f"cp_{uuid.uuid4().hex}"
        channels.pop("checkpoint_id", None)
        changed = {"current_step", "pending_interaction", "turn_status", "ledger_pointer", "state_revision", "checkpoint_uid"}
        # New channel versions prevent blob savers from mutating values shared
        # by earlier snapshots. Scheduling channels and native IDs stay intact.
        new_versions = {key: self.get_next_version(checkpoint["channel_versions"].get(key), None) for key in changed}
        checkpoint["channel_versions"].update(new_versions)
        return checkpoint, new_versions

    def put_writes(self, config, writes, task_id, task_path=""):
        writes = list(writes)
        saved = self.saver.get_tuple(config)
        payload = self._waiting_payload(saved, writes)
        if payload:
            checkpoint, versions = payload
            self.saver.put(saved.parent_config or saved.config, checkpoint, saved.metadata, versions)
        return self.saver.put_writes(config, writes, task_id, task_path)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        writes = list(writes)
        saved = await self.saver.aget_tuple(config)
        payload = self._waiting_payload(saved, writes)
        if payload:
            checkpoint, versions = payload
            await self.saver.aput(saved.parent_config or saved.config, checkpoint, saved.metadata, versions)
        return await self.saver.aput_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id):
        return self.saver.delete_thread(thread_id)

    async def adelete_thread(self, thread_id):
        return await self.saver.adelete_thread(thread_id)
