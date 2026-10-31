<script setup>
// 文字里的 emoji（🟢🟡🔴📖ℹ️📋⚠️）换成定制图标。emoji 仍留在 DOM 里（视觉隐藏），读屏和测试照样读得到原文；
// 没有对应图标文件时原样显示
import { computed } from 'vue'
import { iconUrl } from '../icons'

const props = defineProps({ text: { type: String, default: '' } })
const MAP = { '🟢': 'risk-green', '🟡': 'risk-yellow', '🔴': 'risk-red', '📖': 'knowledge', 'ℹ️': 'guide', '📋': 'plan', '⚠️': 'warning' }
const SPLIT = new RegExp(`(${Object.keys(MAP).join('|')}) ?`, 'u')

// [{ text }] 或 [{ emoji, src }]，emoji 后面紧跟的一个空格并进隐藏文字里
const parts = computed(() => props.text.split(SPLIT).map((seg, i) => {
  if (i % 2 === 0) return { text: seg }
  const src = iconUrl(MAP[seg])
  return src ? { emoji: seg, src } : { text: `${seg} ` }
}).filter((p) => p.emoji || p.text))
</script>

<template>
  <span class="icon-text"><template v-for="(p, i) in parts" :key="i"><template v-if="p.emoji"><img :src="p.src" class="app-icon" alt="" /><span class="sr-only">{{ `${p.emoji} ` }}</span></template><template v-else>{{ p.text }}</template></template></span>
</template>
