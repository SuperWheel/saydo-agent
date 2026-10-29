<script setup>
import DonutChart from './DonutChart.vue'

defineProps({ card: { type: Object, required: true } })

function trend(change) {
  if (!change || /^[+-]0\.0%$/.test(change)) return ''
  return change.startsWith('+') ? 'up' : change.startsWith('-') ? 'down' : ''
}
</script>

<template>
  <div class="card">
    <div class="card-title"><span>{{ card.title }}</span><van-tag plain type="success">🟢 自动查询</van-tag></div>
    <div class="period">{{ card.period }} · 消费合计 <b>¥{{ card.total }}</b>
      <span v-if="card.total_change" class="chg" :class="trend(card.total_change)">较{{ card.compare_to }} {{ card.total_change }}</span>
    </div>
    <DonutChart v-if="card.slices.length" :slices="card.slices" caption="消费" :center="`¥${card.total}`" />
    <div v-else class="empty">这段时间没有消费</div>
    <div v-for="r in card.rows" :key="r.category" class="row">
      <span class="k">{{ r.category }} · {{ r.count }} 笔</span>
      <span class="v">¥{{ r.amount }} <small class="share">{{ r.share }}</small>
        <small class="chg" :class="trend(r.change)">{{ r.change }}</small></span>
    </div>
    <div class="meta">{{ card.note }}</div>
    <div class="meta">数字由系统计算，非 AI 生成 · {{ card.source }} · {{ card.as_of }}</div>
  </div>
</template>

<style scoped>
.period { font-size: 13px; color: var(--muted); }
.period b { color: var(--text); font-variant-numeric: tabular-nums; }
.share { color: var(--muted); margin-left: 4px; }
.chg { margin-left: 4px; font-size: 12px; color: var(--muted); }
.chg.up { color: var(--red); }
.chg.down { color: var(--green); }
.v { font-variant-numeric: tabular-nums; }
.empty { color: var(--muted); font-size: 13px; text-align: center; padding: 10px 0; }
</style>
