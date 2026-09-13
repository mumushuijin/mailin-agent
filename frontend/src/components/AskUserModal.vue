<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Modal, Button, Input, Checkbox } from 'ant-design-vue'
import type { PendingAskUser } from '@/types/chat-ui'

const props = defineProps<{
  pending: PendingAskUser | null
}>()

const emit = defineEmits<{
  answer: [answer: string | string[]]
}>()

const text = ref('')
const selected = ref<string[]>([])

watch(
  () => props.pending,
  () => {
    text.value = ''
    selected.value = []
  },
)

const hasOptions = computed(() => (props.pending?.options.length ?? 0) > 0)
const canSubmit = computed(() => {
  if (!props.pending) return false
  if (hasOptions.value) return selected.value.length > 0
  return text.value.trim().length > 0
})

const submit = () => {
  if (!props.pending || !canSubmit.value) return
  if (hasOptions.value) {
    emit('answer', props.pending.allowMultiple ? [...selected.value] : selected.value[0]!)
    return
  }
  emit('answer', text.value.trim())
}

const toggleOption = (option: string) => {
  if (!props.pending) return
  if (props.pending.allowMultiple) {
    selected.value = selected.value.includes(option)
      ? selected.value.filter((item) => item !== option)
      : [...selected.value, option]
    return
  }
  selected.value = [option]
}
</script>

<template>
  <Modal
    :open="!!pending"
    title="需要你确认一下"
    :closable="false"
    :mask-closable="false"
    :footer="null"
    centered
  >
    <p v-if="pending" class="ask-prompt">{{ pending.prompt }}</p>
    <div v-if="pending && hasOptions" class="ask-options">
      <label
        v-for="option in pending.options"
        :key="option"
        class="ask-option"
        :class="{ selected: selected.includes(option) }"
        @click.prevent="toggleOption(option)"
      >
        <Checkbox
          v-if="pending.allowMultiple"
          :checked="selected.includes(option)"
        />
        <span v-else class="ask-radio" :data-checked="selected.includes(option)" />
        <span>{{ option }}</span>
      </label>
    </div>
    <Input.TextArea
      v-else-if="pending"
      v-model:value="text"
      placeholder="用一句话回答"
      :auto-size="{ minRows: 2, maxRows: 4 }"
    />
    <div class="ask-actions">
      <Button type="primary" :disabled="!canSubmit" @click="submit">提交回答</Button>
    </div>
  </Modal>
</template>

<style scoped>
.ask-prompt {
  margin: 0 0 12px;
  color: var(--color-text);
  line-height: 1.5;
}

.ask-options {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.ask-option {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  cursor: pointer;
  background: var(--color-surface);
}

.ask-option.selected {
  border-color: var(--color-primary, #1677ff);
}

.ask-radio {
  width: 14px;
  height: 14px;
  margin-top: 3px;
  flex: none;
  border: 1px solid var(--color-border);
  border-radius: 50%;
}

.ask-radio[data-checked='true'] {
  border-color: var(--color-primary, #1677ff);
  box-shadow: inset 0 0 0 3px var(--color-primary, #1677ff);
}

.ask-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
</style>
