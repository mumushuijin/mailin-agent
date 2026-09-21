<script setup lang="ts">
import { computed } from 'vue'
import { renderMarkdown } from '@/utils/markdown'
import '@/styles/markdown.css'

const props = withDefaults(
  defineProps<{
    content: string
    streaming?: boolean
  }>(),
  {
    streaming: false,
  },
)

const html = computed(() =>
  renderMarkdown(props.content, { streaming: props.streaming }),
)
</script>

<template>
  <div class="markdown-body" v-html="html"></div>
</template>
