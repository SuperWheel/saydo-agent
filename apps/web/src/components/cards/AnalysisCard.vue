<script setup>
// 自由分析（沙箱）结果卡：后端按模板渲染好的行，金额已格式化成元；有金额的行带 bar_permille 画横条
defineProps({ card: { type: Object, required: true } })

function figures(r) {
  const parts = []
  if (r.count != null) parts.push(`${r.count} 笔`)
  if (r.value != null) parts.push(`${r.value}${r.unit || ''}`)
  return parts.join(' · ')
}
</script>

<template>
  <div class="card">
    <div class="card-title"><span>{{ card.title }}</span><van-tag plain type="success"><IconText text="🟢 沙箱分析" /></van-tag></div>
    <div class="period">{{ card.period }} · 共 {{ card.txn_count }} 笔交易参与分析</div>
    <div v-if="card.question" class="question">「{{ card.question }}」</div>
    <div v-for="(r, i) in card.rows" :key="i" class="row-block">
      <div class="row">
        <span class="k">{{ r.label }}</span>
        <span class="v"><b v-if="r.amount_yuan != null">¥{{ r.amount_yuan }}</b>
          <small v-if="figures(r)" class="fig">{{ figures(r) }}</small></span>
      </div>
      <div v-if="r.bar_permille != null" class="bar"><i :style="{ width: `${r.bar_permille / 10}%` }" /></div>
    </div>
    <div class="meta">{{ card.note }}</div>
    <div class="meta">数据来源：{{ card.source }} · {{ card.as_of }}</div>
  </div>
</template>

<style scoped>
.period { font-size: var(--fs-sm); color: var(--muted); }
.question { font-size: var(--fs-sm); margin: 4px 0 6px; color: var(--ink); }
.row-block { padding: 4px 0; }
.v { font-variant-numeric: tabular-nums; white-space: nowrap; }
.fig { color: var(--muted); margin-left: 6px; }
.bar { height: 4px; background: var(--surface-2); border-radius: 2px; overflow: hidden; margin-top: 3px; }
.bar i { display: block; height: 100%; background: var(--brand); opacity: .55; }
</style>
