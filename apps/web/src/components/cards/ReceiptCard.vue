<script setup>
defineProps({ card: { type: Object, required: true } })
</script>

<template>
  <div class="card receipt" data-testid="receipt-card">
    <div class="watermark">{{ card.watermark }}</div>
    <div class="status" :class="card.status">
      <van-icon :name="card.status === 'succeeded' ? 'checked' : 'warning'" size="28" />
      <span>{{ card.status_label }}</span>
    </div>
    <div v-if="card.amount" class="money center">¥{{ card.amount }}</div>
    <div v-for="[k, v] in card.rows" :key="k" class="row"><span class="k">{{ k }}</span><span class="v">{{ v }}</span></div>
    <div class="meta">回执由交易系统签发，非 AI 生成 · {{ card.source }}</div>
  </div>
</template>

<style scoped>
.receipt { border-top: 3px solid var(--green); }
.watermark {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  color: rgba(200, 16, 46, 0.08); font-size: 26px; font-weight: 800; transform: rotate(-18deg); pointer-events: none;
}
.status { display: flex; align-items: center; justify-content: center; gap: 6px; font-weight: 600; font-size: 16px; color: var(--green); }
.status:not(.succeeded) { color: var(--yellow); }
.center { text-align: center; margin: 6px 0 8px; }
</style>
