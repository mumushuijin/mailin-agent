<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import MailinLogo from '@/components/MailinLogo.vue'

const isMaximized = ref(false)
let cleanup: (() => void) | undefined

onMounted(async () => {
  const win = window.mailin?.window
  if (!win) return

  isMaximized.value = await win.isMaximized()
  cleanup = win.onMaximizedChanged((maximized) => {
    isMaximized.value = maximized
  })
})

onUnmounted(() => {
  cleanup?.()
})

const minimize = () => window.mailin?.window?.minimize()
const maximize = () => window.mailin?.window?.maximize()
const close = () => window.mailin?.window?.close()
</script>

<template>
  <header class="title-bar">
    <div class="title-bar-left">
      <button
        type="button"
        class="traffic-light close"
        title="关闭"
        aria-label="关闭"
        @click="close"
      />
      <button
        type="button"
        class="traffic-light minimize"
        title="最小化"
        aria-label="最小化"
        @click="minimize"
      />
      <button
        type="button"
        class="traffic-light maximize"
        :title="isMaximized ? '还原' : '最大化'"
        :aria-label="isMaximized ? '还原' : '最大化'"
        @click="maximize"
      />
    </div>

    <div class="title-bar-center">
      <MailinLogo :size="18" logo-class="title-logo" />
      <span class="title-text">麦林</span>
    </div>

    <div class="title-bar-right" aria-hidden="true" />
  </header>
</template>

<style scoped>
.title-bar {
  height: 38px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: linear-gradient(180deg, #f3efe4 0%, #ebe6da 100%);
  border-bottom: 1px solid var(--color-border);
  user-select: none;
  -webkit-app-region: drag;
  position: relative;
  z-index: 100;
}

.title-bar-left,
.title-bar-right {
  display: flex;
  align-items: center;
  width: 140px;
  flex-shrink: 0;
}

.title-bar-left {
  gap: 8px;
  padding-left: 14px;
  -webkit-app-region: no-drag;
}

.title-bar-right {
  padding-right: 14px;
}

.title-bar-center {
  position: absolute;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  display: flex;
  align-items: center;
  gap: 6px;
  pointer-events: none;
}

.title-text {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  letter-spacing: 2px;
}

.traffic-light {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  border: none;
  padding: 0;
  cursor: pointer;
  transition: filter 0.15s ease, transform 0.1s ease;
  box-shadow: inset 0 0 0 0.5px rgba(0, 0, 0, 0.12);
}

.traffic-light:hover {
  filter: brightness(0.92);
}

.traffic-light:active {
  transform: scale(0.92);
}

.traffic-light.close {
  background: #ff5f57;
}

.traffic-light.close:hover {
  background: #ff3b30;
}

.traffic-light.minimize {
  background: #febc2e;
}

.traffic-light.minimize:hover {
  background: #f5a623;
}

.traffic-light.maximize {
  background: #28c840;
}

.traffic-light.maximize:hover {
  background: #1fb838;
}
</style>
