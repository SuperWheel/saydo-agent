<script setup>
// 账单报告：所有数字都来自后端计算结果，这里只负责排版，不做任何计算。
// 点卡片打开报告详情（各项指标、分类构成、商户）；「查看消费明细」按卡片上的 detail 取这个区间的全部消费，合计等于报告里的消费。
import { computed, ref } from 'vue'
import BillTxnSheet from './BillTxnSheet.vue'
import CategoryRows from './CategoryRows.vue'
import DonutChart from './DonutChart.vue'

const props = defineProps({ card: { type: Object, required: true } })

// view：'' 关闭；'report' 报告详情；{ category } 消费明细（category 为空 = 全部消费）；from 记住是不是从报告详情点进来的
const view = ref('')
const from = ref('')
const hasDetail = computed(() => !!props.card.detail) // 旧会话里的报告卡没有 detail，就不让点
const hasTxns = computed(() => hasDetail.value && (props.card.rows || []).length > 0)
const query = computed(() => view.value && view.value !== 'report'
  ? { ...props.card.detail, ...(view.value.category ? { category: view.value.category } : {}) } : null)
const kpi = (label) => props.card.kpis.find((k) => k.label === label)?.value

function open(v, origin = '') {
  if (!hasDetail.value || (v !== 'report' && !hasTxns.value)) return
  from.value = origin
  view.value = v
}
</script>

<template>
  <div class="card report" :class="{ tap: hasDetail }" data-testid="report-card" @click="open('report')">
    <div class="card-title"><span>{{ card.title }}</span><van-tag plain type="success"><IconText text="🟢 自动生成" /></van-tag></div>
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

    <div v-if="hasDetail" class="actions">
      <span class="more">点卡片看报告详情 ›</span>
      <van-button v-if="hasTxns" size="small" round plain type="danger" data-testid="report-txns" @click.stop="open({ category: '' })">
        查看消费明细
      </van-button>
    </div>
    <div class="meta">{{ card.note }}</div>
    <div class="meta">报告数字由系统按入账流水计算，非 AI 生成 · {{ card.source }} · {{ card.as_of }}</div>

    <van-popup :show="!!view" position="bottom" round closeable class="sheet-pop" :style="{ height: '86%' }" teleport="body"
               @update:show="(v) => { if (!v) view = '' }">
      <div v-if="view === 'report'" class="sheet-frame" data-testid="report-detail">
        <div class="sheet-head" data-testid="sheet-head">
          <div class="sheet-title">{{ card.title }}</div>
          <div class="sheet-sub">收入 ¥{{ kpi('收入') }} · 消费 ¥{{ kpi('消费') }} · 储蓄率 {{ kpi('储蓄率') }}</div>
        </div>
        <div class="sheet-body">
          <div class="kpis">
            <div v-for="k in card.kpis" :key="k.label" class="kpi">
              <div class="label">{{ k.label }}</div>
              <div class="value">{{ k.unit ? '¥' : '' }}{{ k.value }}</div>
            </div>
          </div>
          <div v-if="card.spend_change" class="line">
            消费较{{ card.compare_to }} <b>{{ card.spend_change }}</b><span v-if="card.spend_change_yuan">（{{ card.spend_change_yuan }} 元）</span>
          </div>
          <template v-if="card.rows.length">
            <div class="sec">分类构成 <small>点某一类看明细</small></div>
            <CategoryRows :rows="card.rows" @pick="(c) => open({ category: c }, 'report')" />
          </template>
          <template v-if="card.top_merchants.length">
            <div class="sec">消费最多的商户</div>
            <div v-for="(m, i) in card.top_merchants" :key="m.name" class="row">
              <span class="k"><i class="rank">{{ i + 1 }}</i>{{ m.name }} · {{ m.count }} 笔</span><span class="v">¥{{ m.amount }}</span>
            </div>
          </template>
          <div class="row"><span class="k">订阅类自动扣费 · {{ card.subscription.count }} 笔</span><span class="v">¥{{ card.subscription.total }}</span></div>
          <div class="row"><span class="k">异常事件</span><span class="v">{{ card.anomalies.length }} 笔</span></div>
          <van-button v-if="hasTxns" block round type="danger" class="all" data-testid="report-all-txns"
                      @click="open({ category: '' }, 'report')">查看全部消费明细</van-button>
          <div class="meta">{{ card.note }}</div>
        </div>
      </div>
      <template v-else-if="query">
        <div v-if="from" class="sheet-back" @click="view = 'report'">‹ 返回报告详情</div>
        <BillTxnSheet :query="query" :period="card.period"
                      :title="view.category ? `${view.category}消费明细` : `${card.period}消费明细`" />
      </template>
    </van-popup>
  </div>
</template>

<style scoped>
.tap { cursor: pointer; }
.actions { display: flex; justify-content: space-between; align-items: center; margin-top: 10px; padding-top: 8px;
  border-top: 1px dashed var(--line); }
.more { font-size: var(--fs-xs); color: var(--brand); }
.sec small { font-weight: 400; color: var(--muted); font-size: var(--fs-xs); margin-left: 4px; }
.all { margin-top: 14px; }
.kpis { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.kpi { background: var(--surface-2); border-radius: var(--radius-sm); padding: 8px 10px; }
.kpi .label { color: var(--muted); font-size: var(--fs-xs); }
.kpi .value { font-size: var(--fs-sheet); font-weight: 700; font-variant-numeric: tabular-nums; margin-top: 2px; }
.line { font-size: var(--fs-sm); color: var(--muted); margin-top: 8px; }
.line b { color: var(--ink); }
.sec { font-size: var(--fs-sm); font-weight: 600; margin: 12px 0 4px; }
.rank { display: inline-block; width: 16px; height: 16px; line-height: 16px; text-align: center; font-style: normal;
  font-size: var(--fs-2xs); border-radius: 50%; background: var(--brand-soft); color: var(--brand); margin-right: 6px; }
.v { font-variant-numeric: tabular-nums; }
.anomaly { background: var(--yellow-soft); border-radius: var(--radius-sm); padding: 8px 10px; margin-top: 6px; }
.anomaly.high { background: var(--red-soft); }
.anomaly .top { display: flex; justify-content: space-between; gap: 8px; font-size: var(--fs-sm); font-weight: 600; }
.anomaly .top span:first-child { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.anomaly small { font-weight: 400; color: var(--muted); }
.anomaly .reason { font-size: var(--fs-xs); line-height: 1.5; margin-top: 2px; }
.none { font-size: var(--fs-sm); color: var(--green-ink); }
.insights { margin: 0; padding-left: 18px; font-size: var(--fs-sm); line-height: 1.7; }
</style>
