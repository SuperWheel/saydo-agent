<script setup>
// 理财对比（F-WM-03）：多列对比 + 风险提示，结论中立。所有数字来自后端。
defineProps({ card: { type: Object, required: true } })
</script>

<template>
  <div class="card cmp">
    <div class="card-title"><span>产品对比</span><van-tag plain type="success">🟢 自动查询</van-tag></div>
    <div class="table" :style="{ gridTemplateColumns: `76px repeat(${card.products.length}, minmax(0, 1fr))` }">
      <div class="h"></div>
      <div v-for="n in card.products" :key="n" class="h name">{{ n }}</div>
      <template v-for="r in card.rows" :key="r.label">
        <div class="label">{{ r.label }}</div>
        <div v-for="(v, i) in r.values" :key="i" class="cell" :class="{ bad: v.includes('不能购买') }">{{ v }}</div>
      </template>
    </div>
    <div class="conclusion">{{ card.conclusion }}</div>
    <div class="meta">{{ card.disclaimer }}</div>
    <div class="meta">数据来源：{{ card.source }} · {{ card.as_of }}</div>
  </div>
</template>

<style scoped>
.cmp { border-top: 3px solid var(--brand); }
.table { display: grid; gap: 0; font-size: 12px; border-top: 1px solid var(--line); }
.table > div { padding: 6px 4px; border-bottom: 1px solid var(--line); overflow-wrap: anywhere; line-height: 1.45; }
.h.name { font-weight: 600; font-size: 13px; }
.label { color: var(--muted); }
.cell.bad { color: var(--red); font-weight: 600; }
.conclusion { background: #f6f7fa; border-radius: 8px; padding: 8px 10px; font-size: 13px; line-height: 1.6; margin-top: 10px; }
</style>
