import assert from 'node:assert/strict'
import test from 'node:test'

import { getToolActionDisplay, getToolConfig, toolResultPreview } from './toolDisplay.ts'

test('known file tools produce compact path-aware action labels', () => {
  const action = getToolActionDisplay(
    'read_file',
    { path: 'frontend/src/views/ChatView.vue' },
    'done',
  )

  assert.equal(action.verb, '已读取')
  assert.equal(action.name, '文件')
  assert.equal(action.target, 'frontend/src/views/ChatView.vue')
  assert.equal(action.targetKind, 'path')
})

test('command tools preserve the command as a scannable target', () => {
  const action = getToolActionDisplay(
    'run_shell',
    { command: 'npm run type-check' },
    'running',
  )

  assert.equal(action.verb, '正在运行')
  assert.equal(action.target, 'npm run type-check')
  assert.equal(action.targetKind, 'command')
})

test('unknown tools fall back without throwing when args are missing', () => {
  const config = getToolConfig('mcp_unknown_tool')
  const action = getToolActionDisplay('mcp_unknown_tool', undefined, 'error')

  assert.equal(config.icon, '🔌')
  assert.equal(action.verb, '已执行')
  assert.equal(action.name, 'tool')
  assert.equal(action.target, undefined)
})

test('result preview collapses multiline and oversized output', () => {
  assert.equal(toolResultPreview('ok\nsecond line'), 'ok')
  assert.equal(toolResultPreview('x'.repeat(130)).length, 118)
})
