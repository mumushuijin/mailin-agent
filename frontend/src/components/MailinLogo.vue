<script setup lang="ts">
withDefaults(
  defineProps<{
    size?: number | string
    logoClass?: string
  }>(),
  { size: 36 },
)

const mascotSrc = '/mailin.png'
</script>

<template>
  <img
    :src="mascotSrc"
    alt="麦林"
    :class="['mailin-logo', logoClass]"
    :width="size"
    :height="size"
    draggable="false"
  />
</template>

<style scoped>
.mailin-logo {
  display: block;
  flex-shrink: 0;
  object-fit: contain;
  border-radius: 8px;
}
</style>
