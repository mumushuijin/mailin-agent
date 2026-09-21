import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))

test('settings router includes dedicated MCP route', () => {
  const source = readFileSync(join(here, 'index.ts'), 'utf8')
  assert.match(source, /path:\s*'mcp'/)
  assert.match(source, /name:\s*'settings-mcp'/)
  assert.match(source, /McpView\.vue/)
  assert.match(source, /path:\s*'\/mcp'/)
  assert.match(source, /redirect:\s*'\/settings\/mcp'/)
})

test('settings router splits markdown 设定 and config 配置', () => {
  const source = readFileSync(join(here, 'index.ts'), 'utf8')
  assert.match(source, /path:\s*'markdown'/)
  assert.match(source, /name:\s*'settings-markdown'/)
  assert.match(source, /SettingsMarkdownView\.vue/)
  assert.match(source, /path:\s*'config'/)
  assert.match(source, /name:\s*'settings-config'/)
  assert.match(source, /ConfigView\.vue/)
  assert.match(source, /redirect:\s*'\/settings\/markdown'/)
})

test('settings nav labels 设定 and 配置 without mixing markdown into modules', () => {
  const settings = readFileSync(join(here, '../views/SettingsView.vue'), 'utf8')
  assert.match(settings, />\s*设定\s*</)
  assert.match(settings, />\s*配置\s*</)
  assert.match(settings, /to="\/settings\/markdown"/)
  assert.match(settings, /to="\/settings\/config"/)

  const configView = readFileSync(join(here, '../views/ConfigView.vue'), 'utf8')
  assert.match(configView, /listModules/)
  assert.match(configView, /isUserVisibleModuleKey/)
  assert.doesNotMatch(configView, /SOUL|USER\.md|HEARTBEAT|MARKDOWN_CONFIG_NAMES/)

  const markdownView = readFileSync(join(here, '../views/SettingsMarkdownView.vue'), 'utf8')
  assert.match(markdownView, /MARKDOWN_CONFIG_NAMES/)
  assert.doesNotMatch(markdownView, /listModules/)
})
