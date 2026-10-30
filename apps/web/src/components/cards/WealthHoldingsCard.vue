<script setup>
// 理财持仓与收益（F-WM-04）：市值、收益都由后端按净值算好，这里只负责排版。
defineProps({ card: { type: Object, required: true } })

const tone = (s) => (s.startsWith('-') ? 'down' : s.startsWith('+') ? 'up' : '')
</script>

<template>
  <div class="card hold">
    <div class="card-title"><span>我的理财</span><van-tag plain type="success">🟢 自动查询</van-tag></div>
    <div class="kpis">
      <div class="kpi"><div class="label">市值合计</div><div class="value">¥{{ card.total_value }}</div></div>
      <div class="kpi"><div class="label">本月收益（{{ card.month }}）</div><div class="value" :class="tone(card.month_income)">{{ card.month_income }}</div></div>
      <div class="kpi"><div class="label">累计收益</div><div class="value" :class="tone(card.total_income)">{{ card.total_income }}</div></div>
      <div class="kpi"><div class="label">投入本金</div><div class="value">¥{{ card.total_principal }}</div></div>
    </div>
    <div v-for="(h, i) in card.items" :key="i" class="item">
      <div class="top"><span>{{ h.name }}</span><van-tag :type="h.status === '可赎回' ? 'success' : 'default'" plain>{{ h.status }}</van-tag></div>
      <div class="sub">{{ h.risk }} · {{ h.purchased_on }} 买入 · 本金 ¥{{ h.principal }}</div>
      <div class="nums">
        <span>市值 ¥{{ h.value }}</span>
        <span :class="tone(h.month_income)">本月 {{ h.month_income }}</span>
        <span :class="tone(h.income)">累计 {{ h.income }}</span>
      </div>
    </div>
    <div v-if="!card.items.length" class="none">目前没有持有理财产品</div>
    <div class="meta">{{ card.note }}</div>
    <div class="meta">{{ card.disclaimer }}</div>
    <div class="meta">数据来源：{{ card.source }} · {{ card.as_of }}</div>
  </div>
</template>

<style scoped>
.hold { border-top: 3px solid var(--brand); }
.kpis { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 8px; }
.kpi { background: #f6f7fa; border-radius: 8px; padding: 8px 10px; }
.kpi .label { color: var(--muted); font-size: 12px; }
.kpi .value { font-size: 16px; font-weight: 700; font-variant-numeric: tabular-nums; margin-top: 2px; }
.item { border-top: 1px dashed var(--line); padding: 8px 0 4px; }
.top { display: flex; justify-content: space-between; align-items: center; font-weight: 600; font-size: 14px; gap: 8px; }
.sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
.nums { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 13px; margin-top: 4px; font-variant-numeric: tabular-nums; }
.up { color: var(--red); }
.down { color: var(--green); }
.none { color: var(--muted); font-size: 13px; text-align: center; padding: 8px 0; }
</style>
