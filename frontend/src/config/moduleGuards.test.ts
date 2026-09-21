import assert from 'node:assert/strict'
import test from 'node:test'

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
  assert.equal(isOrdinaryModuleKey('agent'), true)
  assert.equal(isOrdinaryModuleKey('tools'), true)
  assert.equal(isOrdinaryModuleKey('SOUL'), false)
  assert.equal(isOrdinaryModuleKey('CONFIG'), false)
  assert.equal(isOrdinaryModuleKey('mcp'), false)
})

test('parseJsonText reports syntax errors', () => {
  const bad = parseJsonText('{')
  assert.equal(bad.ok, false)
  if (!bad.ok) {
    assert.equal(bad.error.kind, 'syntax')
  }
  const good = parseJsonText('{"a":1}')
  assert.equal(good.ok, true)
})

test('guardOrdinaryModule rejects wrong agent types and enums', () => {
  const typeFail = guardOrdinaryModule('agent', { max_steps: 'twelve' })
  assert.equal(typeFail.ok, false)
  if (!typeFail.ok) {
    assert.ok(typeFail.errors.some((e) => e.path === '/max_steps'))
  }

  const enumFail = parseModuleUnknown('tools', { enforcement_mode: 'strict' })
  assert.equal(enumFail.ok, false)
  if (!enumFail.ok) {
    assert.ok(enumFail.errors.some((e) => e.path === '/enforcement_mode'))
  }

  const ok = guardOrdinaryModule('agent', {
    model: 'qwen-plus',
    temperature: 0.5,
    max_steps: 10,
    graph_version: '0.1.0',
  })
  assert.equal(ok.ok, true)
})

test('path error mapping normalizes dotted paths', () => {
  assert.equal(normalizeJsonPath('tools.shell.max_timeout'), '/tools/shell/max_timeout')
  assert.equal(normalizeJsonPath('/max_steps'), '/max_steps')
  const mapped = mapPathErrors([
    { path: 'max_steps', kind: 'type', message: '必须是数字' },
  ])
  assert.equal(mapped[0].path, '/max_steps')
  assert.match(mapped[0].message, /\/max_steps/)
})

test('draft restore returns last valid JSON text', () => {
  let draft = createJsonDraft({ model: 'a', temperature: 0.7, max_steps: 8, graph_version: 'x' })
  draft = updateDraftText(draft, '{not-json', 'agent')
  assert.ok(draft.syntaxError)
  assert.equal(draft.dirty, true)

  draft = restoreLastValid(draft)
  assert.equal(draft.syntaxError, null)
  const parsed = parseJsonText(draft.text)
  assert.equal(parsed.ok, true)
  if (parsed.ok) {
    assert.equal((parsed.value as { model: string }).model, 'a')
  }
})

test('formatJson keeps trailing newline for editor stability', () => {
  const text = formatJson({ a: 1 })
  assert.ok(text.endsWith('\n'))
  assert.match(text, /"a": 1/)
})
