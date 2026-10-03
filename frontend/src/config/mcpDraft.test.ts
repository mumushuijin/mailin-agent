import { expect, test } from 'vitest'

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
  expect(missingUrl.ok).toBe(false)

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
  expect(ok.ok).toBe(true)
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
  expect(write.clear_headers).toEqual(['X-Extra'])
  expect(write.connection.headers.Authorization).toBe(MCP_SECRET_MASK)
  expect(write.connection.headers['X-New']).toBe('plain')
  expect('X-Extra' in write.connection.headers).toBe(false)
})

test('copyServerDraft blanks secret values', () => {
  const source = emptyServerDraft()
  source.connection.env = { TOKEN: MCP_SECRET_MASK }
  const copied = copyServerDraft(source)
  expect(copied.connection.env.TOKEN).toBe('')
})

test('clonePlainJson deep-copies nested config values', () => {
  const source = { agent: { model: 'updated', nested: { a: 1 } } }
  const cloned = clonePlainJson(source)
  expect(cloned).not.toBe(source)
  expect(cloned.agent).not.toBe(source.agent)
  cloned.agent.model = 'other'
  expect(source.agent.model).toBe('updated')
})
