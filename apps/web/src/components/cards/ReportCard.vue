<script setup>
// 账单报告：所有数字都来自后端计算结果，这里只负责排版，不做任何计算。
import DonutChart from './DonutChart.vue'

defineProps({ card: { type: Object, required: true } })
</script>

<template>
  <div class="card report">
    <div class="card-title"><span>{{ card.title }}</span><van-tag plain type="success">🟢 自动生成</van-tag></div>
    <div class="kpis">
      <div v-for="k in card.kpis" :key="k.label" class="kpi">
        <div class="label">{{ k.label }}</div>
        <div class="value">{{ k.unit ? '¥' : '' }}{{ k.value }}</div>
      </div>
    </div>
    <div v-if="card.spend_change" class="line">
      消费较{{ card.compare_to }} <b>{{ card.spend_change }}</b><span v-if="card.spend_change_yuan">（{{ card.spend_change_yuan }} 元）</span>
    </div>

    <template v-if="card.slices.length">
      <div class="sec">分类占比</div>
      <DonutChart :slices="card.slices" caption="消费" :center="`¥${card.kpis[1].value}`" />
    </template>

    <template v-if="card.top_merchants.length">
      <div class="sec">消费最多的商户</div>
      <div v-for="(m, i) in card.top_merchants" :key="m.name" class="row">
        <span class="k"><i class="rank">{{ i + 1 }}</i>{{ m.name }} · {{ m.count }} 笔</span><span class="v">¥{{ m.amount }}</span>
      </div>
    </template>
    <div class="row"><span class="k">订阅类自动扣费 · {{ card.subscription.count }} 笔</span><span class="v">¥{{ card.subscription.total }}</span></div>

    <div class="sec">异常事件</div>
    <div v-for="(a, i) in card.anomalies" :key="i" class="anomaly" :class="{ high: a.level === '需要核实' }">
      <div class="top"><span>{{ a.name }}</span><span>¥{{ a.amount }}<small v-if="a.count > 1"> × {{ a.count }}</small></span></div>
      <div class="reason">{{ a.reason }}</div>
    </div>
    <div v-if="!card.anomalies.length" class="none">本期没有发现异常消费</div>

    <template v-if="card.insights.length">
      <div class="sec">本期变化</div>
      <ul class="insights"><li v-for="s in card.insights" :key="s">{{ s }}</li></ul>
    </template>

    <div class="meta">{{ card.note }}</div>
    <div class="meta">报告数字由系统按入账流水计算，非 AI 生成 · {{ card.source }} · {{ card.as_of }}</div>
  </div>
</template>

<style scoped>
.report { border-top: 3px solid var(--brand); }
.kpis { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.kpi { background: #f6f7fa; border-radius: 8px; padding: 8px 10px; }
.kpi .label { color: var(--muted); font-size: 12px; }
.kpi .value { font-size: 17px; font-weight: 700; font-variant-numeric: tabular-nums; margin-top: 2px; }
.line { font-size: 13px; color: var(--muted); margin-top: 8px; }
.line b { color: var(--text); }
.sec { font-size: 13px; font-weight: 600; margin: 12px 0 4px; }
.rank { display: inline-block; width: 16px; height: 16px; line-height: 16px; text-align: center; font-style: normal;
  font-size: 11px; border-radius: 50%; background: var(--brand-soft); color: var(--brand); margin-right: 6px; }
.v { font-variant-numeric: tabular-nums; }
.anomaly { border-left: 3px solid var(--yellow); background: var(--yellow-soft); border-radius: 6px; padding: 6px 8px; margin-top: 6px; }
.anomaly.high { border-left-color: var(--red); background: var(--red-soft); }
.anomaly .top { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; font-weight: 600; }
.anomaly .top span:first-child { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.anomaly small { font-weight: 400; color: var(--muted); }
.anomaly .reason { font-size: 12px; line-height: 1.5; margin-top: 2px; }
.none { font-size: 13px; color: var(--green); }
.insights { margin: 0; padding-left: 18px; font-size: 13px; line-height: 1.7; }
</style>
