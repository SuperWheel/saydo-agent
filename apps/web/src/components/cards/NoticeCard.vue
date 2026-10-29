<script setup>
defineProps({ card: { type: Object, required: true } })
</script>

<template>
  <div class="card notice" :class="card.level">
    <van-icon :name="card.level === 'error' ? 'close' : card.level === 'warning' ? 'warning-o' : 'info-o'" />
    <span>{{ card.text }}</span>
  </div>
</template>

<style scoped>
.notice { display: flex; gap: 8px; align-items: flex-start; font-size: 14px; line-height: 1.5; background: #f4f6fa; }
.notice.warning { background: var(--yellow-soft); color: #874d00; }
.notice.error { background: var(--red-soft); color: var(--red); }
</style>
