<script setup>
// 心愿罐收支明细行（心愿罐明细卡、自动任务卡里点开的弹层共用）：时间、说明、金额。金额字符串由后端带好正负号。
defineProps({ items: { type: Array, required: true } })
const KIND = { deposit: '存入', pay: '付款', refund: '退款', release: '退回活期', withdraw: '转出到活期' }
</script>

<template>
  <div v-for="(t, i) in items" :key="i" class="ptxn" data-testid="pot-txn">
    <div class="l">
      <div class="name">{{ t.label }}</div>
      <div class="at">{{ t.at }}<span class="kind" :class="t.kind">{{ KIND[t.kind] || t.kind }}</span></div>
    </div>
    <div class="amt" :class="t.amount_fen >= 0 ? 'in' : 'out'">{{ t.amount }}</div>
  </div>
  <div v-if="!items.length" class="empty">还没有收支记录</div>
</template>

<style scoped>
.ptxn { display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 8px 0; border-bottom: 1px solid var(--line-soft); }
.l { min-width: 0; }
.name { font-size: var(--fs-body); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.at { font-size: var(--fs-xs); color: var(--muted); margin-top: 2px; font-variant-numeric: tabular-nums; }
.kind { margin-left: 6px; padding: 0 4px; border-radius: var(--radius-xs); background: var(--surface-2); }
.kind.deposit, .kind.refund { background: var(--green-soft); color: var(--green-ink); }
.kind.pay { background: var(--yellow-soft); color: var(--yellow-ink); }
.amt { font-variant-numeric: tabular-nums; font-weight: 600; white-space: nowrap; }
.amt.in { color: var(--green-ink); }
.amt.out { color: var(--ink); }
.empty { color: var(--muted); font-size: var(--fs-sm); text-align: center; padding: 16px 0; }
</style>
