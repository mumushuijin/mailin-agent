import { renderToString } from '@vue/server-renderer'
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { createSSRApp, h } from 'vue'
import AgentStateTimeline from '@/components/AgentStateTimeline.vue'
import ChatMessage from '@/components/ChatMessage.vue'
import { advanceAgentState, createAgentStateStream, finalizeAgentState } from '@/utils/agentStateStream'
import type { AgentStateStream, ChatUiMessage } from '@/types/chat-ui'

const createCompletedState = (
  turnId: number,
  runId: string,
  terminalStatus: 'completed' | 'error',
  terminalLabel: string,
): AgentStateStream => {
  const state = createAgentStateStream(turnId, runId, turnId * 100)
  advanceAgentState(state, 'planning', turnId * 100 + 10)
  advanceAgentState(state, 'tool_calling', turnId * 100 + 20)
  advanceAgentState(state, 'observing', turnId * 100 + 30)
  finalizeAgentState(state, terminalStatus, terminalLabel, turnId * 100 + 40)
  state.currentStep = turnId
  state.todoCompleted = terminalStatus === 'completed' ? 2 : 1
  state.todoTotal = 2
  return state
}

const createProgress = (state: AgentStateStream) => ({
  visible: true,
  active: false,
  stageLabel: state.terminalLabel || '本轮完成',
  elapsedLabel: '1s',
  todoCompleted: state.todoCompleted,
  todoTotal: state.todoTotal,
  terminalLabel: state.terminalLabel,
})

const createAssistantMessage = (
  id: number,
  result: string,
  status: 'done' | 'error',
): ChatUiMessage => ({
  id,
  role: 'assistant',
  content: '最终回答保留在消息正文中。',
  timestamp: new Date('2026-09-25T00:00:00.000Z'),
  segments: [
    {
      type: 'tool',
      id: id * 10,
      tool: 'read_file',
      args: { path: 'src/components/AgentUI.vue', token: 'do-not-show' },
      result,
      status,
    },
    {
      type: 'tool',
      id: id * 10 + 1,
      tool: 'run_shell',
      args: { command: 'npm test' },
      result: '第二个工具独立完成。',
      status: 'done',
    },
    {
      type: 'text',
      id: id * 10 + 2,
      content: '最终回答保留在消息正文中。',
    },
  ],
})

describe('Agent state stream chat integration', () => {
  it('keeps consecutive turns, tool ordering, todo counts, and terminal summaries distinct', async () => {
    const firstState = createCompletedState(1, 'run-1', 'completed', '已完成')
    const secondState = createCompletedState(2, 'run-2', 'error', '执行失败')
    const firstMessage = createAssistantMessage(11, '读取成功。', 'done')
    const secondMessage = createAssistantMessage(12, `${'Error: stack trace\n'.repeat(40)}secret-token`, 'error')

    const app = createSSRApp({
      render: () => h('main', [
        h(AgentStateTimeline, { progress: createProgress(firstState), state: firstState }),
        h(ChatMessage, { message: firstMessage, expandedTools: new Set([110, 111]) }),
        h(AgentStateTimeline, { progress: createProgress(secondState), state: secondState }),
        h(ChatMessage, { message: secondMessage, expandedTools: new Set([120, 121]) }),
      ]),
    })
    const html = (await renderToString(app)).replace(/\s+/g, ' ')

    expect(html.match(/class="run-progress(?: |\")/g)).toHaveLength(2)
    expect(html).toContain('已完成')
    expect(html).toContain('执行失败')
    expect(html).toContain('2/2 个任务完成')
    expect(html).toContain('1/2 个任务完成')
    expect(html.indexOf('src/components/AgentUI.vue')).toBeLessThan(html.indexOf('npm test'))
    expect(html).toContain('失败')
    expect(html).toContain('最终回答保留在消息正文中。')
    expect(html).not.toContain('do-not-show')
    expect(html).not.toContain('secret-token')
  })

  it('keeps narrow-layout contracts on the actual timeline and message components', async () => {
    const state = createCompletedState(3, 'run-3', 'completed', '已完成')
    const message = createAssistantMessage(13, '短结果', 'done')
    const app = createSSRApp({
      render: () => h('section', [
        h(AgentStateTimeline, { progress: createProgress(state), state }),
        h(ChatMessage, { message, expandedTools: new Set([130, 131]) }),
      ]),
    })
    const html = await renderToString(app)
    const timelineSource = readFileSync(new URL('./AgentStateTimeline.vue', import.meta.url), 'utf8')
    const messageSource = readFileSync(new URL('./ChatMessage.vue', import.meta.url), 'utf8')

    expect(html).toMatch(/class="run-progress(?: |\")/)
    expect(html).toContain('class="tool-detail-content"')
    expect(timelineSource).toMatch(/max-width:\s*100%/)
    expect(messageSource).toMatch(/min-width:\s*0/)
    expect(messageSource).toMatch(/overflow-y:\s*auto/)
    expect(messageSource).toMatch(/word-break:\s*break-word/)
  })
})
