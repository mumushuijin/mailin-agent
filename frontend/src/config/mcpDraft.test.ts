import assert from 'node:assert/strict'
import test from 'node:test'

import { clonePlainJson } from './clonePlain.ts'
import { MCP_SECRET_MASK } from './types.ts'
import {
  copyServerDraft,
  draftToServerWrite,
  emptyServerDraft,
  guardMcpServerDraft,
} from './mcpDraft.ts'

test('guardMcpServerDraft requires transport fields', () => {
  const missingUrl = guardMcpServerDraft({
    enabled: true,
    connection: { type: 'streamable-http', headers: {}, args: [], env: {} },
  })
  assert.equal(missingUrl.ok, false)

  const ok = guardMcpServerDraft({
    enabled: true,
    connection: {
      type: 'stdio',
      command: 'npx',
      args: ['-y', 'demo'],
      headers: {},
      env: {},
    },
  })
  assert.equal(ok.ok, true)
})

test('draftToServerWrite clears deleted secrets and keeps masks', () => {
  const baseline = emptyServerDraft()
  baseline.connection.type = 'streamable-http'
  baseline.connection.url = 'https://example.com'
  baseline.connection.headers = { Authorization: MCP_SECRET_MASK, 'X-Extra': MCP_SECRET_MASK }
  baseline.connection.env = {}

  const draft = clonePlainJson(baseline)
  draft.connection.headers = { Authorization: MCP_SECRET_MASK, 'X-New': 'plain' }

  const write = draftToServerWrite(draft, baseline)
  assert.deepEqual(write.clear_headers, ['X-Extra'])
  assert.equal(write.connection.headers.Authorization, MCP_SECRET_MASK)
  assert.equal(write.connection.headers['X-New'], 'plain')
  assert.ok(!('X-Extra' in write.connection.headers))
})

test('copyServerDraft blanks secret values', () => {
  const source = emptyServerDraft()
  source.connection.env = { TOKEN: MCP_SECRET_MASK }
  const copied = copyServerDraft(source)
  assert.equal(copied.connection.env.TOKEN, '')
})

test('clonePlainJson deep-copies nested config values', () => {
  const source = { agent: { model: 'updated', nested: { a: 1 } } }
  const cloned = clonePlainJson(source)
  assert.notEqual(cloned, source)
  assert.notEqual(cloned.agent, source.agent)
  cloned.agent.model = 'other'
  assert.equal(source.agent.model, 'updated')
})
