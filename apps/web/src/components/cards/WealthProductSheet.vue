<script setup>
// 理财产品详情（推荐卡上点产品打开）：净值走势、各期收益、费用、风险揭示，填金额后「申购」。
// 申购不在这里下单：发出 buy 事件，由推荐卡转成 selection 回到对话，后端走和说「买 1 万」同一个提议工具，
// 同样的适当性校验、风险揭示确认卡、人脸 + 短信验证。这里的金额校验只是提前提示，以后端为准。
import { computed, ref, watch } from 'vue'
import { api } from '../../api'

const props = defineProps({ code: { type: String, default: '' } })
const emit = defineEmits(['buy'])

const d = ref(null)
const err = ref('')
const amount = ref('')

watch(() => props.code, async (code) => {
  d.value = null
  err.value = ''
  amount.value = ''
  if (!code) return
  try { d.value = await api.wealthProduct(code) } catch (e) { err.value = e.message }
}, { immediate: true })

const yuan = (fen) => (fen / 100).toLocaleString('zh-CN', { maximumFractionDigits: 0 })
const fen = computed(() => Math.round(Number(amount.value || 0) * 100))
const quick = computed(() => {
  if (!d.value) return []
  const opts = [d.value.min_purchase_fen, 1_000_000, 2_000_000].filter((v, i, a) => a.indexOf(v) === i)
  return opts.filter((v) => v >= d.value.min_purchase_fen && v <= d.value.account.balance_fen).sort((a, b) => a - b)
})
const tip = computed(() => {
  if (!d.value || !amount.value) return ''
  if (fen.value < d.value.min_purchase_fen) return `起购金额 ¥${d.value.min_purchase}`
  if (fen.value > d.value.account.balance_fen) return `可用余额不足（¥${d.value.account.balance}）`
  return ''
})
const canSubmit = computed(() => d.value?.can_buy && fen.value > 0 && !tip.value)

// 净值走势：13 个点（近一年每月一个），纵轴按区间缩放
const line = computed(() => {
  const s = d.value?.series || []
  if (s.length < 2) return ''
  const vs = s.map((p) => p.nav)
  const lo = Math.min(...vs), hi = Math.max(...vs), span = hi - lo || 1
  return s.map((p, i) => `${(i / (s.length - 1)) * 300},${56 - ((p.nav - lo) / span) * 48}`).join(' ')
})

function buy() {
  if (!canSubmit.value) return
  emit('buy', { code: d.value.code, name: d.value.name, amount_text: String(Number(amount.value)), amount: yuan(fen.value) })
}
</script>

<template>
  <div class="sheet" data-testid="wealth-detail">
    <div v-if="err" class="err">{{ err }}</div>
    <van-loading v-else-if="!d" class="loading" />
    <template v-else>
      <div class="head">
        <div class="name">{{ d.name }}</div>
        <div class="tags">
          <van-tag :type="d.risk_n >= 3 ? 'warning' : 'primary'" plain>{{ d.risk }}</van-tag>
          <van-tag plain>{{ d.kind }}</van-tag>
          <van-tag plain>{{ d.term }}</van-tag>
        </div>
        <div class="yield"><span>{{ d.yield_label }}</span><b>{{ d.yield }}</b></div>
        <div class="intro">{{ d.intro }}</div>
      </div>

      <div class="sec">净值走势（近一年）</div>
      <svg viewBox="0 0 300 60" class="spark" preserveAspectRatio="none"><polyline :points="line" /></svg>
      <div class="returns">
        <div v-for="[k, v] in d.returns" :key="k"><span>{{ k }}</span><b>{{ v }}</b></div>
      </div>

      <div class="sec">产品信息</div>
      <div v-for="[k, v] in d.rows" :key="k" class="row"><span class="k">{{ k }}</span><span class="v">{{ v }}</span></div>
      <div class="disclosure">{{ d.disclosure }}</div>

      <div class="sec">申购</div>
      <div v-if="!d.can_buy" class="blocked">{{ d.problem }}</div>
      <template v-else>
        <div class="row"><span class="k">扣款账户</span><span class="v">{{ d.account.name }}（尾号{{ d.account.tail }}）</span></div>
        <div class="row"><span class="k">可用余额</span><span class="v">¥{{ d.account.balance }}</span></div>
        <van-field v-model="amount" type="digit" label="金额（元）" :placeholder="`起购 ${d.min_purchase} 元`" clearable
                   :error-message="tip" data-testid="wealth-amount" />
        <div class="chips">
          <span v-for="v in quick" :key="v" class="chip" @click="amount = String(v / 100)">{{ yuan(v) }}</span>
        </div>
        <van-button type="danger" round block :disabled="!canSubmit" data-testid="wealth-buy" @click="buy">
          申购{{ canSubmit ? ` ¥${yuan(fen)}` : '' }}
        </van-button>
        <div class="note">点申购后会在对话里出确认卡，阅读风险揭示、确认后再做人脸和短信验证，验证通过才扣款。</div>
      </template>
      <div class="meta">{{ d.disclaimer }}</div>
      <div class="meta">数据来源：{{ d.source }} · {{ d.as_of }}</div>
    </template>
  </div>
</template>

<style scoped>
.sheet { padding: 4px 16px 24px; }
.loading { display: flex; justify-content: center; padding: 32px; }
.err { color: var(--red); padding: 24px 0; text-align: center; }
.name { font-size: 17px; font-weight: 600; }
.tags { display: flex; flex-wrap: wrap; gap: 4px; margin: 6px 0; }
.yield { display: flex; align-items: baseline; gap: 8px; font-size: 13px; color: var(--muted); }
.yield b { font-size: 20px; color: var(--brand); font-variant-numeric: tabular-nums; }
.intro { font-size: 13px; line-height: 1.6; margin-top: 6px; }
.sec { font-size: 13px; font-weight: 600; margin: 16px 0 6px; }
.spark { width: 100%; height: 60px; background: #f6f7fa; border-radius: 8px; }
.spark polyline { fill: none; stroke: var(--brand); stroke-width: 2; vector-effect: non-scaling-stroke; }
.returns { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin-top: 8px; text-align: center; }
.returns div { background: #f6f7fa; border-radius: 8px; padding: 6px 0; font-size: 12px; color: var(--muted); }
.returns b { display: block; font-size: 14px; color: var(--text); font-variant-numeric: tabular-nums; }
.disclosure { font-size: 12px; color: var(--red); line-height: 1.6; margin-top: 8px; }
.blocked { font-size: 13px; color: var(--red); background: #fdecee; border-radius: 8px; padding: 8px 10px; line-height: 1.6; }
.chips { display: flex; gap: 6px; margin: 8px 0 12px; }
.chip { font-size: 12px; padding: 3px 10px; border-radius: 12px; background: #f4f5f7; cursor: pointer; }
.note { font-size: 12px; color: var(--muted); line-height: 1.6; margin-top: 8px; }
.meta { font-size: 11px; color: var(--muted); margin-top: 8px; }
</style>
