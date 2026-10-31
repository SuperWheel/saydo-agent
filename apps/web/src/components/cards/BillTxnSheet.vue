<script setup>
// 交易明细详情（消费分类卡、交易查询卡上点开）：区间内符合条件的全部交易，按日期分组，滚到底自动加载下一页。
// 参数照抄卡片上的 detail（区间起止固定在出卡那一刻），笔数、合计、每日小计都由后端按和卡片相同的口径算好。
import { computed, ref, watch } from 'vue'
import { api } from '../../api'

const props = defineProps({
  query: { type: Object, required: true }, // 卡片上的 detail，可再加 category
  title: { type: String, default: '交易明细' },
  period: { type: String, default: '' },
})

const PAGE = 50
const head = ref(null)
const items = ref([])
const days = ref({})
const loading = ref(false)
const finished = ref(false)
const err = ref('')
let seq = 0 // 切换筛选时丢弃上一次还没回来的请求

async function load() {
  const my = seq
  loading.value = true
  try {
    const d = await api.billTxns({ ...props.query, offset: items.value.length, limit: PAGE })
    if (my !== seq) return
    head.value = d
    items.value.push(...d.items)
    days.value = { ...days.value, ...d.day_totals }
    finished.value = !d.has_more
  } catch (e) {
    if (my === seq) { err.value = e.message; finished.value = true }
  } finally {
    if (my === seq) loading.value = false
  }
}

watch(() => JSON.stringify(props.query), () => {
  seq += 1
  head.value = null
  items.value = []
  days.value = {}
  err.value = ''
  finished.value = false
  load()
}, { immediate: true })

const groups = computed(() => {
  const out = []
  for (const t of items.value) {
    if (out.at(-1)?.date !== t.date) out.push({ date: t.date, day: t.day, items: [] })
    out.at(-1).items.push(t)
  }
  return out
})
const net = computed(() => ['消费', '支出'].includes(head.value?.kind)) // 净额口径：退款冲减
</script>

<template>
  <div class="sheet" data-testid="txn-detail">
    <div class="title">{{ title }}</div>
    <div class="period">{{ period }}<span v-for="f in head?.filters" :key="f" class="chip">{{ f }}</span></div>
    <div v-if="head" class="sum">
      <div><span class="k">共 {{ head.count }} 笔，{{ net && head.refund_count ? '净' : '' }}合计</span><b class="money">¥{{ head.total }}</b></div>
      <div v-if="head.refund_count" class="refund-sum">含退款 {{ head.refund_count }} 笔，已冲减 ¥{{ head.refund }}</div>
    </div>
    <div v-if="err" class="err">{{ err }}</div>
    <van-list v-else :loading="loading" :finished="finished" :immediate-check="false" :offset="200"
              :finished-text="head?.count ? `共 ${head.count} 笔，已全部显示` : ''" @load="load">
      <div v-for="g in groups" :key="g.date" class="group">
        <div class="day"><span>{{ g.day }}</span><span v-if="days[g.date]" class="sub">小计 ¥{{ days[g.date] }}</span></div>
        <div v-for="(t, i) in g.items" :key="i" class="txn" data-testid="txn-detail-item">
          <div class="l">
            <div class="name">{{ t.name }}</div>
            <div class="meta-line">{{ t.time }} · {{ t.category }}
              <span v-if="t.kind === '退款'" class="tag back">退款冲减</span>
              <span v-for="x in t.tags" :key="x" class="tag" :class="{ warn: x === '境外' }">{{ x }}</span>
            </div>
          </div>
          <div class="amt" :class="t.direction">{{ t.amount }}</div>
        </div>
      </div>
      <div v-if="head && !head.count" class="empty">这段时间没有符合条件的交易</div>
    </van-list>
    <div v-if="head" class="meta">数字由系统计算，非 AI 生成 · {{ head.source }} · {{ head.as_of }}</div>
  </div>
</template>

<style scoped>
.sheet { padding: 4px 16px 24px; }
.title { font-size: 17px; font-weight: 600; padding-right: 32px; }
.period { font-size: 13px; color: var(--muted); display: flex; flex-wrap: wrap; gap: 4px; align-items: center; margin-top: 4px; }
.chip { background: #f4f6fa; border-radius: 4px; padding: 0 6px; font-size: 12px; }
.sum { background: #f6f7fa; border-radius: 10px; padding: 8px 10px; margin: 10px 0 4px; }
.sum > div:first-child { display: flex; justify-content: space-between; align-items: baseline; }
.sum .k { color: var(--muted); font-size: 13px; }
.sum .money { font-size: 20px; }
.refund-sum { font-size: 12px; color: var(--green); margin-top: 2px; }
.err { color: var(--red); padding: 24px 0; text-align: center; }
.day { position: sticky; top: 0; z-index: 1; display: flex; justify-content: space-between; background: var(--surface);
  font-size: 12px; color: var(--muted); padding: 10px 0 4px; border-bottom: 1px solid var(--line); }
.day .sub { font-variant-numeric: tabular-nums; }
.txn { display: flex; justify-content: space-between; gap: 10px; padding: 8px 0; border-bottom: 1px solid #f3f4f7; }
.l { min-width: 0; }
.name { font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.meta-line { font-size: 12px; color: var(--muted); margin-top: 2px; }
.tag { margin-left: 4px; padding: 0 4px; border-radius: 3px; background: #f4f6fa; }
.tag.warn { background: var(--yellow-soft); color: var(--yellow); }
.tag.back { background: var(--green-soft); color: var(--green); }
.amt { font-variant-numeric: tabular-nums; font-weight: 600; white-space: nowrap; }
.amt.in { color: var(--green); }
.empty { color: var(--muted); font-size: 13px; text-align: center; padding: 16px 0; }
.meta { font-size: 11px; color: var(--muted); margin-top: 8px; }
</style>
