<script setup>
// 轻量环形图：纯 SVG，不引图表库。占比（permille）由后端算好，这里只换算成弧长，不产生任何展示数字。
import { computed } from 'vue'
import { colorOf } from './colors'

const props = defineProps({
  slices: { type: Array, required: true }, // [{ category, share, permille, amount, merged }]
  center: { type: String, default: '' },
  caption: { type: String, default: '' },
})

const R = 40
const C = 2 * Math.PI * R

const arcs = computed(() => {
  let offset = 0
  return props.slices.filter(s => s.permille > 0).map(s => {
    const len = (s.permille / 1000) * C
    const arc = { ...s, color: colorOf(s.category), dash: `${len} ${C - len}`, offset: -offset }
    offset += len
    return arc
  })
})
</script>

<template>
  <div class="donut">
    <svg viewBox="0 0 100 100" class="ring" role="img" :aria-label="`分类占比：${slices.map(s => s.category + s.share).join('，')}`">
      <circle cx="50" cy="50" :r="R" fill="none" stroke="var(--line)" stroke-width="14" />
      <circle v-for="a in arcs" :key="a.category" cx="50" cy="50" :r="R" fill="none" :stroke="a.color" stroke-width="14"
              :stroke-dasharray="a.dash" :stroke-dashoffset="a.offset" transform="rotate(-90 50 50)" />
      <text v-if="caption" x="50" y="45" text-anchor="middle" class="cap">{{ caption }}</text>
      <text x="50" :y="caption ? 59 : 54" text-anchor="middle" class="center">{{ center }}</text>
    </svg>
    <ul class="legend">
      <li v-for="a in arcs" :key="a.category">
        <i :style="{ background: a.color }" />
        <span class="name">{{ a.category }}<small v-if="a.merged?.length">（含{{ a.merged.join('、') }}）</small></span>
        <span class="share">{{ a.share }}</span>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.donut { display: flex; align-items: center; gap: 12px; margin: 4px 0 6px; }
.ring { width: 112px; height: 112px; flex-shrink: 0; }
.cap { font-size: 8px; fill: var(--muted); }
.center { font-size: 10px; font-weight: 700; fill: var(--text); font-variant-numeric: tabular-nums; }
.legend { list-style: none; margin: 0; padding: 0; flex: 1; min-width: 0; font-size: 13px; }
.legend li { display: flex; align-items: center; gap: 6px; padding: 2px 0; }
.legend i { width: 8px; height: 8px; border-radius: 2px; flex-shrink: 0; }
.legend .name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.legend small { color: var(--muted); }
.legend .share { font-variant-numeric: tabular-nums; color: var(--muted); }
</style>
