<script setup>
import { icon } from '../../icons'

defineProps({ card: { type: Object, required: true } })
</script>

<template>
  <div class="card receipt" data-testid="receipt-card">
    <div class="watermark">{{ card.watermark }}</div>
    <div class="status" :class="card.status">
      <van-icon :name="card.status === 'succeeded' ? icon('receipt-success', 'checked') : icon('receipt-pending', 'warning')" size="22" />
      <span>{{ card.status_label }}</span>
    </div>
    <div v-if="card.amount" class="money center">¥{{ card.amount }}</div>
    <div v-for="[k, v] in card.rows" :key="k" class="row"><span class="k">{{ k }}</span><span class="v">{{ v }}</span></div>
    <div class="meta">回执由交易系统签发，非 AI 生成 · {{ card.source }}</div>
  </div>
</template>

<style scoped>
.watermark {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  color: var(--brand); opacity: 0.07; font-size: 26px; font-weight: 800; transform: rotate(-18deg); pointer-events: none;
}
.status {
  display: flex; align-items: center; gap: var(--space-2);
  margin: calc(-1 * var(--space-4)) calc(-1 * var(--space-4)) var(--space-3); padding: 11px var(--space-4);
  font-weight: 600; font-size: var(--fs-title); line-height: 22px;
  background: var(--green-tint); color: var(--green-ink);
}
.status .van-icon { color: var(--green-ink); }
.status:not(.succeeded) { background: var(--yellow-tint); color: var(--yellow-ink); }
.status:not(.succeeded) .van-icon { color: var(--yellow-ink); }
.center { text-align: center; margin: var(--space-1) 0 10px; padding-bottom: 10px; border-bottom: 1px solid var(--line); }
</style>
