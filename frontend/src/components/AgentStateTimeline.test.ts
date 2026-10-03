import { renderToString } from '@vue/server-renderer'
import { describe, expect, it } from 'vitest'
import { createSSRApp, h } from 'vue'
import AgentStateTimeline from '@/components/AgentStateTimeline.vue'
import {
  advanceAgentState,
  createAgentStateStream,
  finalizeAgentState,
} from '@/utils/agentStateStream'

describe('AgentStateTimeline', () => {
  it('renders active macro state, todo progress, and stage trail without stale step count', async () => {
    const state = createAgentStateStream(1, 'run-1', 100)
    advanceAgentState(state, 'planning', 120)
    advanceAgentState(state, 'tool_calling', 140)
    state.currentStep = 2
    state.maxSteps = 4
    state.todoCompleted = 3
    state.todoTotal = 8

    const app = createSSRApp(AgentStateTimeline, {
      progress: {
        visible: true,
        active: true,
        stageLabel: '正在执行操作…',
        elapsedLabel: '4s',
        todoCompleted: 3,
        todoTotal: 8,
      },
      state,
    })
    const html = await renderToString(app)
    const normalizedText = html.replace(/<[^>]*>/g, '').replace(/\s+/g, ' ')

    expect(html).toContain('正在执行操作')
    expect(html).toContain('3/8 个任务完成')
    expect(normalizedText).not.toContain('步骤 2/4')
    expect(html).toContain('正在拆解任务')
    expect(html).toContain('正在执行操作')
    expect(html).toContain('查看过程')
    expect((html.match(/class="run-progress-stage"/g) || []).length).toBe(1)
  })

  it('renders terminal summaries without active styling semantics', async () => {
    const state = createAgentStateStream(2, 'run-2', 100)
    finalizeAgentState(state, 'completed', '本轮完成', 300)

    const app = createSSRApp(AgentStateTimeline, {
      progress: {
        visible: true,
        active: false,
        stageLabel: '本轮完成',
        elapsedLabel: '2s',
        todoCompleted: 1,
        todoTotal: 1,
        terminalLabel: '已完成',
      },
      state,
    })
    const html = await renderToString(app)

    expect(html).toContain('本轮完成')
    expect(html).toContain('已完成')
    expect(html).toContain('run-progress terminal')
    expect(html).toContain('查看过程')
    expect((html.match(/class="run-progress-stage"/g) || []).length).toBe(1)
  })

  it('renders cancelled and handoff states as terminal summaries', async () => {
    for (const status of ['cancelled', 'handoff'] as const) {
      const state = createAgentStateStream(3, `run-${status}`, 100)
      finalizeAgentState(state, status, status === 'cancelled' ? '本轮已取消' : '已交还用户', 300)
      const app = createSSRApp(AgentStateTimeline, {
        progress: {
          visible: true,
          active: false,
          stageLabel: status === 'cancelled' ? '本轮已取消' : '已交还用户',
          elapsedLabel: '2s',
          todoCompleted: 0,
          todoTotal: 0,
          terminalLabel: status === 'cancelled' ? '已取消' : '已交还',
        },
        state,
      })
      const html = await renderToString(app)
      expect(html).toContain('run-progress terminal')
      expect(html).toContain(status === 'cancelled' ? '本轮已取消' : '已交还用户')
      expect(html).not.toContain('progress-pulse')
    }
  })

  it('keeps distinct same-status checkpoint steps in the timeline', async () => {
    const state = createAgentStateStream(5, 'run-5', 100)
    state.nodes.push(
      { id: 'checkpoint:agent-1', stepId: 'agent-1', kind: 'phase', status: 'reasoning', label: 'Agent 执行', startedAt: 110, endedAt: 130, durationMs: 20, active: false, summary: '已检查请求' },
      { id: 'checkpoint:tools-1', stepId: 'tools-1', kind: 'tool', status: 'observing', label: '工具执行', startedAt: 130, endedAt: 180, durationMs: 50, active: false, summary: '读取文件' },
      { id: 'checkpoint:agent-2', stepId: 'agent-2', kind: 'phase', status: 'reasoning', label: 'Agent 执行', startedAt: 180, active: true, summary: '整理结果' },
    )
    const app = createSSRApp(AgentStateTimeline, {
      progress: { visible: true, active: true, stageLabel: '正在思考中…', elapsedLabel: '1s', todoCompleted: 0, todoTotal: 0 },
      state,
    })
    const html = await renderToString(app)
    expect(html).toContain('Agent 执行')
    expect(html).toContain('工具执行')
    expect(html).toContain('读取文件')
    expect(html).toContain('50ms')
  })

  it('separates turn elapsed metadata from message status context', async () => {
    const state = createAgentStateStream(4, 'run-4', 100)
    advanceAgentState(state, 'tool_calling', 120)
    const progress = {
      visible: true,
      active: true,
      stageLabel: '正在执行操作…',
      elapsedLabel: '4s',
      todoCompleted: 1,
      todoTotal: 2,
    }

    const app = createSSRApp({
      render: () => [
        h(AgentStateTimeline, { progress, state, mode: 'time' }),
        h(AgentStateTimeline, { progress, state, mode: 'message' }),
      ],
    })
    const html = await renderToString(app)

    expect((html.match(/用时 4s/g) || []).length).toBe(1)
    expect((html.match(/正在执行操作/g) || []).length).toBeGreaterThanOrEqual(1)
    expect(html).toContain('1/2 个任务完成')
  })
})
