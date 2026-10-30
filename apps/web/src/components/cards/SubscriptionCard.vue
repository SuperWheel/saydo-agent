<script setup>
// 订阅总览（sub_overview）和临期扣费（sub_upcoming）共用。数字全部由服务端算好，商户名只在卡片上展示。
import { computed } from 'vue'

const props = defineProps({ card: { type: Object, required: true } })
const upcoming = computed(() => props.card.type === 'sub_upcoming')
const STATE_TAG = { active: 'success', billing_stopped: 'default', overdue: 'warning' }
</script>

<template>
  <div class="card">
    <div class="card-title"><span>{{ card.title }}</span><van-tag plain type="success">🟢 自动查询</van-tag></div>

    <div v-if="!upcoming" class="totals">
      <div><div class="k">预计每月</div><div class="money">¥{{ card.monthly_total }}</div></div>
      <div><div class="k">一年约</div><div class="year">¥{{ card.yearly_total }}</div></div>
      <div><div class="k">订阅</div><div class="year">{{ card.count }} 个</div></div>
    </div>
    <div v-else class="totals">
      <div><div class="k">待扣合计</div><div class="money">¥{{ card.total }}</div></div>
      <div><div class="k">笔数</div><div class="year">{{ card.count }} 笔</div></div>
      <div><div class="k">借记卡余额</div><div class="year" :class="{ short: !card.balance_enough }">{{ card.balance_enough ? '够扣' : '可能不足' }}</div></div>
    </div>

    <div v-for="(s, i) in card.items" :key="i" class="item" :class="s.state">
      <div class="top">
        <span class="name">{{ s.name }}</span>
        <span class="amt">¥{{ s.amount }}<small> / {{ s.cycle.replace('每', '') }}</small></span>
      </div>
      <div class="sub">
        <van-tag :type="STATE_TAG[s.state]" plain>{{ s.state_label }}</van-tag>
        <span>{{ s.pay_via }}</span>
        <span v-for="t in s.tags" :key="t" class="tag">{{ t }}</span>
      </div>
      <div class="next">{{ s.state === 'billing_stopped' ? s.next : `下次扣费：${s.next}` }}</div>
      <div v-if="!upcoming" class="conf">识别置信度 {{ s.confidence }}</div>
    </div>
    <div v-if="!card.count" class="empty">{{ upcoming ? `未来 ${card.within_days} 天没有订阅要扣费` : '没有识别到订阅或周期性自动扣费' }}</div>

    <div v-if="card.to_confirm?.length" class="extra">
      <div class="extra-title">像是周期扣费，但把握不大（未计入合计）</div>
      <div v-for="(s, i) in card.to_confirm" :key="i" class="extra-row">{{ s.name }} · {{ s.cycle }} ¥{{ s.amount }} · 置信度 {{ s.confidence }}</div>
    </div>
    <div v-if="card.excluded?.length" class="extra">
      <div v-for="(e, i) in card.excluded" :key="i" class="extra-row">已排除：{{ e.name }}（{{ e.reason }}）</div>
    </div>
    <div class="meta">{{ card.note || '扣费日按周期推算，以商户实际扣款为准' }} · {{ card.source }} · {{ card.as_of }}</div>
    <div class="meta">想停掉某个订阅，直接说「取消 xx 会员」</div>
  </div>
</template>

<style scoped>
.totals { display: grid; grid-template-columns: 1.3fr 1fr 0.8fr; gap: 8px; align-items: end; margin-bottom: 4px; }
.totals .k { color: var(--muted); font-size: 12px; }
.year { font-size: 16px; font-weight: 600; font-variant-numeric: tabular-nums; }
.year.short { color: var(--red); }
.item { border-left: 3px solid var(--green); background: var(--green-soft); border-radius: 6px; padding: 8px 10px; margin-top: 8px; }
.item.billing_stopped { border-left-color: var(--muted); background: #f5f6f8; }
.item.overdue { border-left-color: var(--yellow); background: var(--yellow-soft); }
.top { display: flex; justify-content: space-between; gap: 8px; font-weight: 600; font-size: 14px; }
.name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.amt { font-variant-numeric: tabular-nums; white-space: nowrap; }
.amt small { font-weight: 400; color: var(--muted); }
.sub { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 4px; font-size: 12px; color: var(--muted); }
.tag { background: var(--yellow-soft); color: #874d00; border-radius: 3px; padding: 0 4px; }
.next { font-size: 13px; margin-top: 4px; }
.conf { font-size: 12px; color: var(--muted); margin-top: 2px; }
.empty { color: var(--muted); font-size: 14px; text-align: center; padding: 10px 0; }
.extra { margin-top: 10px; font-size: 12px; color: var(--muted); line-height: 1.6; }
.extra-title { color: var(--text); font-weight: 600; }
</style>
