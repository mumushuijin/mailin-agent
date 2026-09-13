<script setup lang="ts">
import { computed } from 'vue'
import { Tooltip } from 'ant-design-vue'
import type { ApiUsage, ContextUsage } from '@/api/chat'

const props = defineProps<{
  contextUsage: ContextUsage
  apiUsage: ApiUsage | null
  compressing: boolean
}>()

const RING_SIZE = 40
const RING_RADIUS = 16
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS

const contextUsagePercent = computed(() => {
  return Math.min(100, Math.round(props.contextUsage.ratio * 100))
})

const contextUsageLevel = computed(() => {
  const ratio = props.contextUsage.ratio ?? 0
  if (ratio > 0.8) return 'danger'
  if (ratio > 0.6) return 'warning'
  return 'normal'
})

const contextUsageLines = computed(() => {
  const u = props.contextUsage
  const max = u.max_tokens.toLocaleString()
  const total = u.total_tokens.toLocaleString()
  const hasApi = (props.apiUsage?.prompt_tokens ?? 0) > 0
  const lines = [`上下文窗口 ${contextUsagePercent.value}%`]

  if (hasApi) {
    lines.push(`有效 ${total} / ${max}（API+增量）`)
    const api = props.apiUsage!
    const prompt = (api.agent?.prompt_tokens ?? api.prompt_tokens).toLocaleString()
    const completion = api.completion_tokens.toLocaleString()
    lines.push(`API ${prompt} + ${completion} out`)
    if (u.estimated_tokens != null) {
      lines.push(`粗估 ${u.estimated_tokens.toLocaleString()}`)
    }
  } else {
    lines.push(`粗估 ${total} / ${max}`)
  }

  const api = props.apiUsage
  if (api?.prompt_cache_hit_tokens && api.prompt_cache_hit_tokens > 0) {
    lines.push(`缓存 ${api.prompt_cache_hit_tokens.toLocaleString()}`)
  }

  if (props.compressing) lines.push('正在整理上下文…')
  else if (u.warning) lines.push(u.warning)
  return lines
})

const contextUsageTooltip = computed(() => contextUsageLines.value.join('\n'))

const contextRingOffset = computed(() => {
  const ratio = Math.min(1, props.contextUsage.ratio ?? 0)
  return RING_CIRCUMFERENCE * (1 - ratio)
})

const contextRingColor = computed(() => {
  if (props.compressing) return 'var(--color-primary)'
  const level = contextUsageLevel.value
  if (level === 'danger') return 'var(--color-primary)'
  if (level === 'warning') return '#faad14'
  return '#52c41a'
})
</script>

<template>
  <Tooltip
    placement="top"
    overlay-class-name="context-usage-tooltip"
    :overlay-inner-style="{ textAlign: 'left', padding: '8px 12px', maxWidth: '280px' }"
  >
    <template #title>
      <div class="context-usage-tooltip-lines">
        <div
          v-for="(line, index) in contextUsageLines"
          :key="index"
          class="context-usage-tooltip-line"
        >
          {{ line }}
        </div>
      </div>
    </template>
    <div
      class="context-usage-ring"
      :class="[contextUsageLevel, { compressing }]"
      :aria-label="contextUsageTooltip"
    >
      <svg :width="RING_SIZE" :height="RING_SIZE" :viewBox="`0 0 ${RING_SIZE} ${RING_SIZE}`">
        <circle
          class="ring-track"
          :cx="RING_SIZE / 2"
          :cy="RING_SIZE / 2"
          :r="RING_RADIUS"
          fill="none"
          stroke-width="3"
        />
        <circle
          class="ring-progress"
          :cx="RING_SIZE / 2"
          :cy="RING_SIZE / 2"
          :r="RING_RADIUS"
          fill="none"
          stroke-width="3"
          :stroke="contextRingColor"
          :stroke-dasharray="RING_CIRCUMFERENCE"
          :stroke-dashoffset="contextRingOffset"
          stroke-linecap="round"
          :transform="`rotate(-90 ${RING_SIZE / 2} ${RING_SIZE / 2})`"
        />
      </svg>
      <span class="ring-label">{{ contextUsagePercent }}%</span>
    </div>
  </Tooltip>
</template>

<style scoped>
.context-usage-ring {
  position: relative;
  flex-shrink: 0;
  width: 40px;
  height: 40px;
  cursor: default;
}

.context-usage-ring.compressing {
  animation: ring-pulse 1.2s ease-in-out infinite;
}

.context-usage-ring .ring-track {
  stroke: var(--color-border);
}

.context-usage-ring .ring-progress {
  transition: stroke-dashoffset 0.35s ease, stroke 0.25s ease;
}

.context-usage-ring .ring-label {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 9px;
  font-weight: 600;
  color: var(--color-text-secondary);
  line-height: 1;
  pointer-events: none;
}

.context-usage-ring.warning .ring-label {
  color: #b8860b;
}

.context-usage-ring.danger .ring-label {
  color: var(--color-primary);
}

@keyframes ring-pulse {
  0%, 100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.75;
    transform: scale(1.05);
  }
}

.context-usage-tooltip-line + .context-usage-tooltip-line {
  margin-top: 4px;
}
</style>
