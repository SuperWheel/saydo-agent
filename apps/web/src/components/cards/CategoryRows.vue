<script setup>
// 分类构成（消费分类卡、账单报告卡点开后共用）：每类金额、笔数、占比条、环比，点某一类看该类明细。数字都来自卡片。
import { colorOf, trend } from './colors'

defineProps({ rows: { type: Array, required: true } })
const emit = defineEmits(['pick'])
</script>

<template>
  <div v-for="r in rows" :key="r.category" class="cat" data-testid="category-row" @click="emit('pick', r.category)">
    <div class="top">
      <span class="name"><i :style="{ background: colorOf(r.category) }" />{{ r.category }}<small>{{ r.count }} 笔</small></span>
      <span class="amt">¥{{ r.amount }}<span class="go">›</span></span>
    </div>
    <div class="bar"><div :style="{ width: r.share, background: colorOf(r.category) }" /></div>
    <div class="sub"><span>占比 {{ r.share }}</span><span class="chg" :class="trend(r.change)">环比 {{ r.change }}</span></div>
  </div>
</template>

<style scoped>
.cat { padding: 10px 0; border-bottom: 1px solid var(--line-soft); cursor: pointer; }
.cat .top { display: flex; justify-content: space-between; align-items: center; font-size: var(--fs-body); }
.cat .name { display: flex; align-items: center; gap: 6px; }
.cat .name i { width: 10px; height: 10px; border-radius: var(--radius-xs); }
.cat .name small { color: var(--muted); font-size: var(--fs-xs); }
.cat .amt { font-weight: 600; font-variant-numeric: tabular-nums; }
.cat .go { color: var(--muted); margin-left: 6px; font-weight: 400; }
.bar { height: 6px; background: var(--surface-2); border-radius: var(--radius-xs); margin: 6px 0 4px; overflow: hidden; }
.bar div { height: 100%; border-radius: var(--radius-xs); }
.cat .sub { display: flex; justify-content: space-between; font-size: var(--fs-xs); color: var(--muted); }
.chg { margin-left: 4px; font-size: var(--fs-xs); color: var(--muted); }
.chg.up { color: var(--red); }
.chg.down { color: var(--green-ink); }
</style>
