<script setup>
// 金额滚轮（免密额度用）：万 / 千 / 百 / 十 / 个五列，每列 0–9，v-model 是整数元（0–99999）。
// 超过 max 或父组件传了 error 时数字标红；提交按钮由父组件禁用并写明原因。
// 手机上直接拨动；电脑上可以点上下相邻的数字，或在某一列上滚动鼠标滚轮逐位加减。
import { computed, ref, watch } from 'vue'

const props = defineProps({
  modelValue: { type: Number, default: 0 },
  max: { type: Number, default: 99999 }, // 上限（元）
  label: { type: String, default: '' },
  error: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])

const PLACES = ['万', '千', '百', '十', '个']
const columns = PLACES.map(() => Array.from({ length: 10 }, (_, i) => ({ text: String(i), value: i })))
const toDigits = (n) => String(Math.max(0, Math.min(99999, Math.floor(n || 0)))).padStart(5, '0').split('').map(Number)
const toNumber = (ds) => ds.reduce((s, d) => s * 10 + (Number(d) || 0), 0)

const digits = ref(toDigits(props.modelValue))
watch(() => props.modelValue, (n) => { if (toNumber(digits.value) !== n) digits.value = toDigits(n) })
watch(digits, (ds) => { const n = toNumber(ds); if (n !== props.modelValue) emit('update:modelValue', n) })

const over = computed(() => props.modelValue > props.max)
const fmt = (n) => n.toLocaleString('en-US')

// 鼠标滚轮：按列逐位加减（触控板一次会发很多事件，攒够一格再动）
let acc = 0
function onWheel(e) {
  const col = e.target.closest?.('.van-picker-column')
  if (!col) return
  e.preventDefault()
  acc += e.deltaY
  if (Math.abs(acc) < 40) return
  const i = [...col.parentElement.children].filter((c) => c.classList.contains('van-picker-column')).indexOf(col)
  const step = acc > 0 ? 1 : -1
  acc = 0
  if (i < 0) return
  const next = digits.value.slice()
  next[i] = Math.max(0, Math.min(9, next[i] + step))
  digits.value = next
}
</script>

<template>
  <div class="wheel" :class="{ bad: over || error }" data-testid="digit-wheel">
    <div class="head">
      <span class="label">{{ label }}</span>
      <span class="val"><b data-testid="digit-wheel-value">{{ fmt(modelValue) }}</b> 元</span>
    </div>
    <div class="places"><span v-for="p in PLACES" :key="p">{{ p }}</span></div>
    <div class="pick" @wheel="onWheel">
      <van-picker v-model="digits" :columns="columns" :show-toolbar="false" :option-height="34" :visible-option-num="3" />
    </div>
    <div class="cap">上限 {{ fmt(max) }} 元</div>
  </div>
</template>

<style scoped>
.wheel { border: 1px solid var(--line); border-radius: var(--radius-md); padding: 8px 10px 6px; background: var(--surface); min-width: 0; }
.wheel.bad { border-color: var(--red); background: var(--red-soft); }
.head { display: flex; justify-content: space-between; align-items: baseline; gap: var(--space-2); }
.label { font-size: var(--fs-sm); color: var(--muted); }
.val { font-size: var(--fs-sm); color: var(--muted); white-space: nowrap; }
.val b { font-size: var(--fs-money-md); color: var(--ink); font-variant-numeric: tabular-nums; }
.bad .val b { color: var(--red); }
.places { display: flex; margin-top: 4px; }
.places span { flex: 1; text-align: center; font-size: var(--fs-2xs); color: var(--muted); }
.pick { border-radius: var(--radius-sm); overflow: hidden; }
.pick :deep(.van-picker) { --van-picker-option-font-size: 18px; --van-picker-option-padding: 0; }
.pick :deep(.van-picker-column) { min-width: 0; font-variant-numeric: tabular-nums; }
.pick :deep(.van-picker__frame) { left: 0; right: 0; background: var(--surface-2); border-radius: var(--radius-sm); z-index: 0; }
.pick :deep(.van-hairline-unset--top-bottom::after) { display: none; }
.pick :deep(.van-picker-column__wrapper) { position: relative; z-index: 1; }
.bad .pick :deep(.van-picker-column__item--selected) { color: var(--red); font-weight: 600; }
.pick :deep(.van-picker-column__item--selected) { font-weight: 600; }
.cap { font-size: var(--fs-2xs); color: var(--muted); text-align: right; margin-top: 2px; }
.bad .cap { color: var(--red-ink); }
</style>
