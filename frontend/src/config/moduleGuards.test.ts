import { expect, test } from 'vitest'

import {
  createJsonDraft,
  formatJson,
  mapPathErrors,
  normalizeJsonPath,
  parseJsonText,
  restoreLastValid,
  updateDraftText,
} from './jsonDraft.ts'
import {
  guardOrdinaryModule,
  isOrdinaryModuleKey,
  parseModuleUnknown,
} from './moduleGuards.ts'

test('isOrdinaryModuleKey accepts registry keys only', () => {
  expect(isOrdinaryModuleKey('agent')).toBe(true)
  expect(isOrdinaryModuleKey('tools')).toBe(true)
  expect(isOrdinaryModuleKey('SOUL')).toBe(false)
  expect(isOrdinaryModuleKey('CONFIG')).toBe(false)
  expect(isOrdinaryModuleKey('mcp')).toBe(false)
})

test('parseJsonText reports syntax errors', () => {
  const bad = parseJsonText('{')
  expect(bad.ok).toBe(false)
  if (!bad.ok) {
    expect(bad.error.kind).toBe('syntax')
  }
  const good = parseJsonText('{"a":1}')
  expect(good.ok).toBe(true)
})

test('guardOrdinaryModule rejects wrong agent types and enums', () => {
  const typeFail = guardOrdinaryModule('agent', { max_steps: 'twelve' })
  expect(typeFail.ok).toBe(false)
  if (!typeFail.ok) {
    expect(typeFail.errors.some((e) => e.path === '/max_steps')).toBe(true)
  }

  const enumFail = parseModuleUnknown('tools', { enforcement_mode: 'strict' })
  expect(enumFail.ok).toBe(false)
  if (!enumFail.ok) {
    expect(enumFail.errors.some((e) => e.path === '/enforcement_mode')).toBe(true)
  }

  const ok = guardOrdinaryModule('agent', {
    model: 'qwen-plus',
    temperature: 0.5,
    max_steps: 10,
    graph_version: '0.1.0',
  })
  expect(ok.ok).toBe(true)
})

test('path error mapping normalizes dotted paths', () => {
  expect(normalizeJsonPath('tools.shell.max_timeout')).toBe('/tools/shell/max_timeout')
  expect(normalizeJsonPath('/max_steps')).toBe('/max_steps')
  const mapped = mapPathErrors([
    { path: 'max_steps', kind: 'type', message: '必须是数字' },
  ])
  expect(mapped[0].path).toBe('/max_steps')
  expect(mapped[0].message).toMatch(/\/max_steps/)
})

test('draft restore returns last valid JSON text', () => {
  let draft = createJsonDraft({ model: 'a', temperature: 0.7, max_steps: 8, graph_version: 'x' })
  draft = updateDraftText(draft, '{not-json', 'agent')
  expect(draft.syntaxError).toBeTruthy()
  expect(draft.dirty).toBe(true)

  draft = restoreLastValid(draft)
  expect(draft.syntaxError).toBeNull()
  const parsed = parseJsonText(draft.text)
  expect(parsed.ok).toBe(true)
  if (parsed.ok) {
    expect((parsed.value as { model: string }).model).toBe('a')
  }
})

test('formatJson keeps trailing newline for editor stability', () => {
  const text = formatJson({ a: 1 })
  expect(text.endsWith('\n')).toBe(true)
  expect(text).toMatch(/"a": 1/)
})
