import assert from 'node:assert/strict'
import test from 'node:test'

import {
  clearMarkdownCache,
  renderMarkdown,
  stabilizeStreamingMarkdown,
} from './markdown.ts'

test('fenced code with language exposes lang label structure', () => {
  clearMarkdownCache()
  const html = renderMarkdown(['```ts', 'const x = 1', '```'].join('\n'))
  assert.match(html, /class="markdown-code"/)
  assert.match(html, /data-lang="ts"/)
  assert.match(html, /markdown-code__lang">ts</)
  assert.match(html, /const x = 1/)
})

test('untagged fence still renders as code block', () => {
  clearMarkdownCache()
  const html = renderMarkdown(['```', 'plain', '```'].join('\n'))
  assert.match(html, /class="markdown-code"/)
  assert.doesNotMatch(html, /data-lang=/)
  assert.match(html, /<pre><code>/)
  assert.match(html, /plain/)
})

test('external links get target and rel', () => {
  clearMarkdownCache()
  const html = renderMarkdown('[docs](https://example.com/path)')
  assert.match(html, /href="https:\/\/example\.com\/path"/)
  assert.match(html, /target="_blank"/)
  assert.match(html, /rel="noopener noreferrer"/)
})

test('javascript links are not navigable', () => {
  clearMarkdownCache()
  const html = renderMarkdown('[x](javascript:alert(1))')
  assert.doesNotMatch(html, /href=["']javascript:/i)
  assert.match(html, />x</)
})

test('remote images are not auto-loaded by default', () => {
  clearMarkdownCache()
  const html = renderMarkdown('![shot](https://evil.example/a.png)')
  assert.doesNotMatch(html, /<img\b/i)
  assert.doesNotMatch(html, /src=["']https:\/\/evil\.example/)
  assert.match(html, /markdown-image-placeholder/)
  assert.match(html, /\[shot\]/)
})

test('task lists render disabled checkboxes', () => {
  clearMarkdownCache()
  const html = renderMarkdown(['- [x] done', '- [ ] todo'].join('\n'))
  assert.match(html, /type="checkbox"/)
  assert.ok(html.includes('disabled=""'))
  assert.ok(html.includes('checked=""'))
  assert.match(html, /done/)
  assert.match(html, /todo/)
})

test('tables are wrapped for horizontal scroll', () => {
  clearMarkdownCache()
  const html = renderMarkdown(['| a | b |', '| - | - |', '| 1 | 2 |'].join('\n'))
  assert.match(html, /markdown-table-wrap/)
  assert.match(html, /<table>/)
})

test('script payloads are stripped', () => {
  clearMarkdownCache()
  const html = renderMarkdown('<script>alert(1)</script>\n\nhello')
  assert.doesNotMatch(html, /<script/i)
  assert.doesNotMatch(html, /alert\(1\)/)
  assert.match(html, /hello/)
})

test('streaming incomplete fence is stabilized and not cached', () => {
  clearMarkdownCache()
  const partial = ['```ts', 'const x = 1'].join('\n')
  assert.equal(stabilizeStreamingMarkdown(partial).endsWith('```'), true)

  const streamingHtml = renderMarkdown(partial, { streaming: true })
  assert.match(streamingHtml, /markdown-code/)
  assert.match(streamingHtml, /const x = 1/)

  // 流式结果不入缓存：同一原文在非流式下仍会重新解析并缓存终态
  const finalSrc = `${partial}\n\`\`\``
  const finalHtml = renderMarkdown(finalSrc)
  assert.match(finalHtml, /markdown-code/)
  assert.equal(renderMarkdown(finalSrc), finalHtml)
})

test('stable historical text hits LRU cache', () => {
  clearMarkdownCache()
  const src = '**bold** and `code`'
  const first = renderMarkdown(src)
  const second = renderMarkdown(src)
  assert.equal(first, second)
  assert.match(first, /<strong>bold<\/strong>/)
})
