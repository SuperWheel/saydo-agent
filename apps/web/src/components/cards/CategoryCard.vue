<script setup>
// 消费分类（F-BA-03）。点卡片打开分类构成（每类金额、占比、笔数、环比），点某一类看该类明细；
// 「查看消费明细」直接看这个区间的全部消费。明细按卡片上的 detail 取数，合计和卡片一致。
import { computed, ref } from 'vue'
import BillTxnSheet from './BillTxnSheet.vue'
import CategoryRows from './CategoryRows.vue'
import DonutChart from './DonutChart.vue'
import { trend } from './colors'

const props = defineProps({ card: { type: Object, required: true } })

// view：'' 关闭；'cats' 分类构成；{ category } 某一类明细（category 为空 = 全部消费）；from 记住是不是从分类构成点进来的
const view = ref('')
const from = ref('')
const hasDetail = computed(() => !!props.card.detail && props.card.rows.length > 0)
const query = computed(() => view.value && view.value !== 'cats'
  ? { ...props.card.detail, ...(view.value.category ? { category: view.value.category } : {}) } : null)

function open(v, origin = '') {
  if (!hasDetail.value) return
  from.value = origin
  view.value = v
}
</script>

<template>
  <div class="card" :class="{ tap: hasDetail }" data-testid="category-card" @click="open('cats')">
    <div class="card-title"><span>{{ card.title }}</span><van-tag plain type="success"><IconText text="🟢 自动查询" /></van-tag></div>
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
    <div v-if="hasDetail" class="actions">
      <span class="more">点卡片看分类构成 ›</span>
      <van-button size="small" round plain type="danger" data-testid="category-txns" @click.stop="open({ category: '' })">
        查看消费明细
      </van-button>
    </div>
    <div class="meta">{{ card.note }}</div>
    <div class="meta">数字由系统计算，非 AI 生成 · {{ card.source }} · {{ card.as_of }}</div>

    <van-popup :show="!!view" position="bottom" round closeable class="sheet-pop" :style="{ height: '86%' }" teleport="body"
               @update:show="(v) => { if (!v) view = '' }">
      <div v-if="view === 'cats'" class="sheet-frame" data-testid="category-detail">
        <div class="sheet-head" data-testid="sheet-head">
          <div class="sheet-title">{{ card.period }}消费构成</div>
          <div class="sheet-sum"><span class="k">消费合计（已扣除退款）</span><b class="money">¥{{ card.total }}</b></div>
          <div v-if="card.total_change" class="vs">较{{ card.compare_to }} <span class="chg" :class="trend(card.total_change)">{{ card.total_change }}</span></div>
        </div>
        <div class="sheet-body">
          <CategoryRows :rows="card.rows" @pick="(c) => open({ category: c }, 'cats')" />
          <van-button block round type="danger" class="all" @click="open({ category: '' }, 'cats')">查看全部消费明细</van-button>
          <div class="meta">{{ card.note }}</div>
        </div>
      </div>
      <template v-else-if="query">
        <div v-if="from" class="sheet-back" @click="view = 'cats'">‹ 返回分类构成</div>
        <BillTxnSheet :query="query" :period="card.period"
                      :title="view.category ? `${view.category}消费明细` : '消费明细'" />
      </template>
    </van-popup>
  </div>
</template>

<style scoped>
.tap { cursor: pointer; }
.period { font-size: var(--fs-sm); color: var(--muted); }
.period b { color: var(--ink); font-variant-numeric: tabular-nums; }
.share { color: var(--muted); margin-left: 4px; }
.chg { margin-left: 4px; font-size: var(--fs-xs); color: var(--muted); }
.chg.up { color: var(--red); }
.chg.down { color: var(--green-ink); }
.v { font-variant-numeric: tabular-nums; }
.empty { color: var(--muted); font-size: var(--fs-sm); text-align: center; padding: 10px 0; }
.actions { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; padding-top: 8px;
  border-top: 1px dashed var(--line); }
.more { font-size: var(--fs-xs); color: var(--brand); }

.vs { font-size: var(--fs-xs); color: var(--muted); margin-top: 4px; }
.all { margin-top: 14px; }
.meta { font-size: var(--fs-2xs); color: var(--muted); margin-top: 8px; }
</style>
