import { describe, expect, it } from 'vitest'
import { isCanonicalAgentState, readCanonicalAgentState } from './canonicalAgentState'
import { StreamEventInbox } from './streamEventInbox'
import { buildHistoryMessages, buildHistoryTimelines } from './historyProjection'
import { validateHistory } from '@/api/session'

export const checkpoint = (revision = 0, turn = 'turn_1') => ({
  schema_version: 2 as const, checkpoint_id: `cp_${'a'.repeat(32)}`, state_revision: revision,
  scope: { workspace_id: 'ws_1', session_id: 's1', turn_id: turn },
  context: { working_message: [] }, max_step_every_turn: 48, tasks: [], current_task: null,
  current_step: { step_id: null, step_name: 'agent', step_status: 'pending', tool_calls: [] },
  memory: { recorded_count: 0, last_synced_turn: null, source_files: [] },
  turn_status: 'created', ledger_pointer: { applied_seq: 500 },
})

describe('new checkpoint and history protocol', () => {
  it('accepts a greeting without a synthetic task and rejects old and ambiguous identity', () => {
    expect(isCanonicalAgentState(checkpoint())).toBe(true)
    expect(isCanonicalAgentState({ ...checkpoint(), current_step: { ...checkpoint().current_step, model: null } })).toBe(true)
    for (const invalid of [
      { ...checkpoint(), schema_version: 1 },
      { ...checkpoint(), run_id: 'old' },
      { ...checkpoint(), scope: { ...checkpoint().scope, step_id: null } },
      { ...checkpoint(), current_task: 'missing' },
      { ...checkpoint(), turn_status: 'succeeded' },
      { ...checkpoint(), ledger_pointer: { file: 'ledger.jsonl', applied_seq: 1 } },
      { ...checkpoint(), current_step: { ...checkpoint().current_step, tool_calls: [{ call_id: 'c1', status: 'done', input: {} }] } },
    ]) expect(() => readCanonicalAgentState(invalid)).toThrow('不支持')
  })

  it('keeps ledger cursor, revision and transport ordering independent, and requests a missing revision', () => {
    const inbox = new StreamEventInbox()
    inbox.push({ type: 'session', turn_id: 'turn_1', seq: 1, state: checkpoint(1) })
    expect(inbox.push({ type: 'stage', seq: 2, turn_id: 'turn_1', state: checkpoint(3) }).gapDetected).toBe(true)
    const sync = inbox.push({ type: 'state_sync', seq: 3, turn_id: 'turn_1', state: checkpoint(3) })
    expect(sync.events).toHaveLength(1)
    expect(sync.events[0]?.state?.ledger_pointer?.applied_seq).toBe(500)
    expect(inbox.push({ type: 'stage', seq: 4, turn_id: 'turn_1', state: checkpoint(2) }).events).toHaveLength(0)
  })

  it('rejects cross session events and duplicate terminals even when transport IDs differ', () => {
    const inbox = new StreamEventInbox()
    inbox.push({ type: 'session', turn_id: 'turn_1', session_id: 's1', seq: 1 })
    expect(inbox.push({ type: 'tool_start', turn_id: 'turn_1', session_id: 's2', seq: 2 }).events).toHaveLength(0)
    expect(inbox.push({ type: 'done', turn_id: 'turn_1', seq: 2 }).events).toHaveLength(1)
    expect(inbox.push({ type: 'done', turn_id: 'turn_1', seq: 3, event_id: 'another' }).events).toHaveLength(0)
  })

  it('rebuilds one tool with failed attempts and a truthful final outcome, without rendering audit or reasoning blocks', () => {
    const timelines = [{ turn_id: 'turn_1', status: 'completed', tools: [{ call_id: 'c1', definition_seq: 2, result_seq: 5, status: 'succeeded', attempts: [{ attempt: 1, status: 'failed' }, { attempt: 2, status: 'succeeded' }] }] }]
    const messages = buildHistoryMessages([
      { role: 'user', content: '你好', message_id: 'm1', turn_id: 'turn_1', seq: 1, timestamp: 1791964800000 },
      { role: 'assistant', message_id: 'm2', turn_id: 'turn_1', seq: 2, tool_calls: [{ id: 'c1', name: 'run_shell', arguments: { command: 'pytest', token: 'secret' } }] },
      { role: 'system', content: 'internal lifecycle', seq: 3 },
      { role: 'tool', tool_call_id: 'c1', turn_id: 'turn_1', seq: 5, tool_status: 'succeeded' },
      { role: 'assistant', message_id: 'm3', turn_id: 'turn_1', seq: 6, content: [{ type: 'reasoning', text: 'hidden' }, { type: 'text', text: 'ok' }] },
    ], timelines)
    expect(messages).toHaveLength(3)
    const tool = messages[1]?.segments?.[0]
    expect(tool?.type).toBe('tool')
    if (tool?.type === 'tool') {
      expect(tool.status).toBe('succeeded'); expect(tool.attempts).toHaveLength(2)
      expect(tool.args.token).toBe('[已脱敏]'); expect(tool.resultSeq).toBe(5)
    }
    expect(messages[2]?.content).toBe('ok')
    expect(messages[0]?.timestamp.getTime()).toBe(1791964800000)
    expect(buildHistoryTimelines(messages, timelines)[1]?.status).toBe('completed')
    expect(() => validateHistory({ session_id: 's1', messages: [], projection_version: 2 } as never)).toThrow('不支持')
  })
})
