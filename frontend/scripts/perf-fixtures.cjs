const fs = require('node:fs')
const path = require('node:path')
const { performance } = require('node:perf_hooks')

const LONG_HISTORY_COUNT = 2_000
const INITIAL_PAGE_SIZE = 30
const DENSE_EVENT_COUNT = 1_000
const LONG_HISTORY_BUDGET_MS = 80
const DENSE_STREAM_BUDGET_MS = 50

function buildLongHistory(count = LONG_HISTORY_COUNT) {
  return Array.from({ length: count }, (_, index) => ({
    id: `msg-${index}`,
    role: index % 2 === 0 ? 'user' : 'assistant',
    content: `message-${index}`,
    timestamp: index,
  }))
}

function pageNewest(messages, limit = INITIAL_PAGE_SIZE, before = null) {
  const end = before == null ? messages.length : Math.max(0, Math.min(messages.length, Number(before)))
  const start = Math.max(0, end - limit)
  return {
    messages: messages.slice(start, end),
    has_more: start > 0,
    next_cursor: start > 0 ? String(start) : null,
  }
}

async function applyDenseStreamFixture(count = DENSE_EVENT_COUNT) {
  const queue = []
  let scheduled = false
  let flushes = 0
  let content = ''

  const flush = () => {
    scheduled = false
    flushes += 1
    while (queue.length) {
      content += queue.shift().content
    }
  }

  const handle = (event) => {
    queue.push(event)
    if (!scheduled) {
      scheduled = true
      setImmediate(flush)
    }
  }

  for (let index = 0; index < count; index += 1) {
    handle({ type: 'chunk', content: String(index % 10) })
  }

  await new Promise((resolve) => setImmediate(resolve))
  return { flushes, contentLength: content.length }
}

async function main() {
  const history = buildLongHistory()
  const historyStarted = performance.now()
  const firstPage = pageNewest(history)
  const historyMs = performance.now() - historyStarted

  if (firstPage.messages.length !== INITIAL_PAGE_SIZE || firstPage.messages[0].id !== 'msg-1970') {
    throw new Error('Long-history fixture did not return the newest page first')
  }
  if (!firstPage.has_more || firstPage.next_cursor !== '1970') {
    throw new Error('Long-history fixture did not expose cursor metadata')
  }

  const denseStarted = performance.now()
  const dense = await applyDenseStreamFixture()
  const denseMs = performance.now() - denseStarted

  if (dense.contentLength !== DENSE_EVENT_COUNT) {
    throw new Error('Dense-stream fixture lost chunk content')
  }
  if (dense.flushes > 2) {
    throw new Error(`Dense-stream fixture flushed too often: ${dense.flushes}`)
  }

  const report = {
    long_history: {
      count: LONG_HISTORY_COUNT,
      initial_page_size: INITIAL_PAGE_SIZE,
      elapsed_ms: Number(historyMs.toFixed(2)),
      budget_ms: LONG_HISTORY_BUDGET_MS,
      passed: historyMs <= LONG_HISTORY_BUDGET_MS,
    },
    dense_stream: {
      event_count: DENSE_EVENT_COUNT,
      flushes: dense.flushes,
      elapsed_ms: Number(denseMs.toFixed(2)),
      budget_ms: DENSE_STREAM_BUDGET_MS,
      passed: denseMs <= DENSE_STREAM_BUDGET_MS,
    },
  }

  const outputDir = path.join(__dirname, '..', '.vite')
  fs.mkdirSync(outputDir, { recursive: true })
  fs.writeFileSync(
    path.join(outputDir, 'perf-fixtures.json'),
    JSON.stringify(report, null, 2),
    'utf8',
  )

  if (!report.long_history.passed || !report.dense_stream.passed) {
    throw new Error(`Performance fixture budget failed: ${JSON.stringify(report)}`)
  }

  console.log(JSON.stringify(report, null, 2))
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
