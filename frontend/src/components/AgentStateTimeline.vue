<script setup lang="ts">
import { computed } from 'vue'
import { AGENT_STATUS_META, getAgentStateTrail } from '@/utils/agentStateStream'
import type { AgentStateStream, RunProgressSummary } from '@/types/chat-ui'

interface Props {
  progress: RunProgressSummary
  state?: AgentStateStream | null
  mode?: 'full' | 'time' | 'message'
}

const props = withDefaults(defineProps<Props>(), {
  state: null,
  mode: 'full',
})

const mode = computed(() => props.mode)
const trailNodes = computed(() => getAgentStateTrail(props.state, 4))
const currentMeta = computed(() => AGENT_STATUS_META[props.state?.status || 'idle'])

function durationLabel(durationMs?: number): string {
  if (typeof durationMs !== 'number' || durationMs < 0) return ''
  return durationMs < 1000 ? `${durationMs}ms` : `${(durationMs / 1000).toFixed(1)}s`
}
</script>

<template>
  <div
    :class="[
      'run-progress',
      { active: progress.active && !currentMeta.terminal, terminal: currentMeta.terminal },
      `run-progress-${mode}`,
    ]"
    aria-live="polite"
  >
    <div class="run-progress-main">
      <span v-if="mode !== 'time'" class="run-progress-icon" aria-hidden="true">{{ currentMeta.icon }}</span>
      <span v-if="mode !== 'time'" class="run-progress-indicator" aria-hidden="true"></span>
      <span v-if="mode !== 'time'" class="run-progress-stage">{{ progress.stageLabel }}</span>
      <code v-if="mode !== 'message'" class="inline-code">用时 {{ progress.elapsedLabel }}</code>
      <code v-if="mode !== 'time' && progress.todoTotal > 0" class="inline-code">
        {{ progress.todoCompleted }}/{{ progress.todoTotal }} 个任务完成
      </code>
      <span v-if="mode !== 'time' && progress.terminalLabel" class="run-progress-terminal">
        {{ progress.terminalLabel }}
      </span>
    </div>
    <details v-if="mode === 'full' && trailNodes.length" class="agent-state-history">
      <summary>查看过程 · {{ trailNodes.length }}</summary>
      <div class="agent-state-trail">
        <span
          v-for="node in trailNodes"
          :key="node.id"
          :class="['agent-state-chip', node.status]"
          :title="node.summary || node.label"
        >{{ node.label }}<template v-if="durationLabel(node.durationMs)"> · {{ durationLabel(node.durationMs) }}</template></span>
      </div>
    </details>
  </div>
</template>

<style scoped>
.run-progress {
  align-self: flex-start;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  max-width: 100%;
  padding: 7px 12px;
  margin-bottom: 6px;
  color: var(--color-text-secondary);
  background: color-mix(in srgb, var(--color-surface) 88%, var(--color-primary-light));
  border: 1px solid var(--color-border);
  border-radius: 8px;
  font-size: 12px;
}

.run-progress.active {
  border-color: var(--color-primary-border);
}

.run-progress.terminal {
  background: color-mix(in srgb, var(--color-surface) 94%, var(--color-border));
}

.run-progress-main {
  display: flex;
  align-items: center;
  flex: 1 1 240px;
  flex-wrap: wrap;
  min-width: 0;
  gap: 8px;
}

.run-progress-icon {
  flex: none;
  font-size: 13px;
  line-height: 1;
}

.run-progress-indicator {
  width: 7px;
  height: 7px;
  flex: none;
  border-radius: 50%;
  background: var(--color-text-secondary);
}

.run-progress.active .run-progress-indicator {
  background: var(--color-primary);
  animation: progress-pulse 1.4s ease-in-out infinite;
}

.run-progress-time {
  padding-block: 4px;
  margin-bottom: 2px;
  background: transparent;
  border-color: transparent;
}

.run-progress-message {
  margin-bottom: 4px;
  padding-block: 5px;
}

.run-progress-stage {
  min-width: 0;
  overflow-wrap: anywhere;
  color: var(--color-text);
  font-weight: 600;
}

.run-progress-terminal {
  overflow-wrap: anywhere;
  color: var(--color-text-secondary);
}

.agent-state-history {
  flex: 0 1 auto;
  min-width: 0;
  color: var(--color-text-secondary);
}

.agent-state-history summary {
  cursor: pointer;
  list-style: none;
  color: var(--color-text-secondary);
  font-size: 11px;
  user-select: none;
}

.agent-state-history summary::-webkit-details-marker {
  display: none;
}

.agent-state-history summary::before {
  content: '›';
  display: inline-block;
  margin-right: 3px;
  transition: transform 0.15s ease;
}

.agent-state-history[open] summary::before {
  transform: rotate(90deg);
}

.agent-state-trail {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  max-width: 100%;
  padding-top: 5px;
}

.agent-state-chip {
  display: inline-flex;
  align-items: center;
  max-width: 180px;
  padding: 2px 6px;
  border: 1px solid var(--color-border);
  border-radius: 999px;
  color: var(--color-text-secondary);
  background: color-mix(in srgb, var(--color-surface) 85%, var(--color-border));
  font-size: 11px;
  line-height: 1.3;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-state-chip.active {
  color: var(--color-primary);
  border-color: var(--color-primary-border);
  background: var(--color-primary-light);
}

.agent-state-chip.completed {
  color: var(--color-success, #237804);
}

.agent-state-chip.error {
  color: var(--color-danger, #cf1322);
}

.agent-state-chip.cancelled {
  color: var(--color-warning, #ad6800);
}

.agent-state-chip.handoff {
  color: var(--color-text-secondary);
}

@media (max-width: 560px) {
  .run-progress {
    align-items: flex-start;
    gap: 6px;
    padding: 7px 9px;
  }

  .run-progress-main {
    flex-basis: 100%;
    align-items: flex-start;
  }

  .run-progress-stage {
    flex: 1 1 120px;
  }

  .agent-state-history {
    flex-basis: 100%;
    padding-left: 21px;
  }
}

@keyframes progress-pulse {
  0%, 100% { opacity: 0.45; transform: scale(0.85); }
  50% { opacity: 1; transform: scale(1); }
}
</style>
