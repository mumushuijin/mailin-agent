import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { chatApi } from './chat'
import { chatWs, mapWsToStreamEvent } from './ws'

vi.mock('./index', () => ({ default: { post: vi.fn() }, getStreamApiBase: () => 'http://localhost', getWsApiBaseAsync: async () => 'ws://localhost' }))

beforeEach(() => { vi.restoreAllMocks() })
afterEach(() => { vi.unstubAllGlobals() })

function streamResponse(parts: string[]): Response {
  return new Response(new ReadableStream({ start(controller) {
    for (const part of parts) controller.enqueue(new TextEncoder().encode(part))
    controller.close()
  } }))
}

test('rejects missing and unknown stream versions in WS and SSE before delivering content', async () => {
  for (const version of [undefined, 2, 99]) {
    expect(() => mapWsToStreamEvent({ type: 'chunk', data: { content: 'unsafe' }, stream_schema_version: version })).toThrow('stream_schema_version=3')
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResponse([
      `event: chunk\ndata: ${JSON.stringify({ content: 'unsafe', stream_schema_version: version })}\n\n`,
    ])))
    const callback = vi.fn()
    await expect(chatApi.sendMessageStream('hello', 's1', callback)).rejects.toThrow('stream_schema_version=3')
    expect(callback).not.toHaveBeenCalled()
  }
})

test('keeps the SSE event name when the network splits an event from its data', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResponse([
    'event: chunk\n', 'data: {"stream_schema_version":3,"turn_id":"turn-1","content":"hello"}\n\n',
  ])))
  const callback = vi.fn()
  const response = await chatApi.sendMessageStream('hello', 's1', callback)
  expect(callback).toHaveBeenCalledWith(expect.objectContaining({ type: 'chunk', content: 'hello' }))
  expect(response.content).toBe('hello')
})

test('reuses turn, submission, branch and fork identity when WS falls back to SSE', async () => {
  const ws = vi.spyOn(chatWs, 'sendMessage').mockRejectedValue(new Error('not connected'))
  const sse = vi.spyOn(chatApi, 'sendMessageStream').mockResolvedValue({ session_id: 's1', content: 'ok' })
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  const callback = vi.fn()
  const options = { branch_id: 'child', fork_cursor: { branch_id: 'main', event_seq: 4, event_id: 'event-4' } }
  await chatApi.sendMessageWs('hello', 's1', callback, undefined, options)
  const args = ws.mock.calls[0]!
  expect(args[3]).toEqual(expect.any(String))
  expect(args[4]).toEqual(expect.any(String))
  expect(sse).toHaveBeenCalledWith('hello', 's1', callback, undefined, args[3], args[4], options)
})

test('does not submit again after WS execution has started or cancellation', async () => {
  const ws = vi.spyOn(chatWs, 'sendMessage').mockRejectedValue(Object.assign(new Error('failed'), { executionStarted: true }))
  const sse = vi.spyOn(chatApi, 'sendMessageStream')
  await expect(chatApi.sendMessageWs('hello', 's1', vi.fn())).rejects.toThrow('failed')
  expect(sse).not.toHaveBeenCalled()
  const controller = new AbortController()
  ws.mockImplementation(async () => { controller.abort(); throw new Error('closed') })
  await expect(chatApi.sendMessageWs('hello', 's1', vi.fn(), controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
  expect(sse).not.toHaveBeenCalled()
})
