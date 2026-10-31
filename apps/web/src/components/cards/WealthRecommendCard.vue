<script setup>
// 理财推荐（F-WM-02）：只列风险等级不高于客户等级的产品。分数、收益参考、过滤原因都来自后端，这里只负责排版。
// 点产品打开详情，详情里填金额申购：转成 selection 回到对话，和说「买第一个，1万」走同一条申购流程。
import { ref } from 'vue'
import WealthProductSheet from './WealthProductSheet.vue'

defineProps({ card: { type: Object, required: true } })
const emit = defineEmits(['select'])
const open = ref('')

function buy(b) {
  open.value = ''
  emit('select', { label: `申购${b.name}`, detail: `¥${b.amount}`,
                   selection: { type: 'wealth_buy', code: b.code, amount_text: b.amount_text } })
}
</script>

<template>
  <div class="card rec">
    <div class="card-title"><span>为你筛选的理财产品</span><van-tag plain type="success"><IconText text="🟢 自动查询" /></van-tag></div>
    <div class="cond">
      风险承受能力 <b>{{ card.profile.level }}</b> · 可购 {{ card.profile.range }}<span v-if="card.condition"> · {{ card.condition }}</span>
    </div>
    <div v-for="(p, i) in card.items" :key="p.code" class="item" data-testid="wealth-item" @click="open = p.code">
      <div class="top">
        <span class="name"><i class="rank">{{ i + 1 }}</i>{{ p.name }}</span>
        <span class="score">{{ p.score }} 分</span>
      </div>
      <div class="tags">
        <van-tag :type="p.risk_n >= 3 ? 'warning' : 'primary'" plain>{{ p.risk }}</van-tag>
        <van-tag plain>{{ p.kind }}</van-tag>
        <van-tag plain>{{ p.term }}</van-tag>
      </div>
      <div class="row"><span class="k">{{ p.yield_label }}</span><span class="v">{{ p.yield }}</span></div>
      <div class="row"><span class="k">起购金额</span><span class="v">¥{{ p.min_purchase }}</span></div>
      <ul class="why"><li v-for="r in p.reasons" :key="r">{{ r }}</li></ul>
      <div class="risk">{{ p.risk_note }}</div>
      <div class="more">查看详情 · 申购 ›</div>
    </div>
    <div v-if="card.items.length" class="how">点产品查看详情并申购，也可以直接说「买第一个，1万」。</div>
    <div v-if="card.diff" class="diff">{{ card.diff }}</div>
    <template v-if="card.filtered.length">
      <div class="sec">没有推荐的产品</div>
      <div v-for="f in card.filtered" :key="f.name" class="row small"><span class="k">{{ f.name }}</span><span class="v">{{ f.reason }}</span></div>
    </template>
    <div class="meta">打分权重：{{ card.weights }}</div>
    <div class="meta">{{ card.disclaimer }}</div>
    <div class="meta">数据来源：{{ card.source }} · {{ card.as_of }}</div>
    <van-popup :show="!!open" position="bottom" round closeable :style="{ maxHeight: '86%' }" teleport="body"
               @update:show="(v) => { if (!v) open = '' }">
      <WealthProductSheet :code="open" @buy="buy" />
    </van-popup>
  </div>
</template>

<style scoped>
.cond { font-size: var(--fs-sm); color: var(--muted); margin-bottom: 8px; }
.cond b { color: var(--ink); }
.item { border: 1px solid var(--line); border-radius: var(--radius-md); padding: 12px; cursor: pointer; transition: border-color .15s, background .15s; }
.item:active { border-color: var(--brand); background: var(--brand-soft); }
.item + .item { margin-top: 8px; }
.more { font-size: var(--fs-xs); color: var(--brand); text-align: right; margin-top: 6px; }
.how { font-size: var(--fs-xs); color: var(--muted); margin-top: 8px; }
.top { display: flex; justify-content: space-between; align-items: center; font-weight: 600; font-size: var(--fs-body); }
.score { color: var(--brand); font-variant-numeric: tabular-nums; }
.rank { display: inline-block; width: 18px; height: 18px; line-height: 18px; text-align: center; font-style: normal;
  font-size: var(--fs-2xs); border-radius: 50%; background: var(--brand); color: var(--on-brand); margin-right: 6px; }
.tags { display: flex; flex-wrap: wrap; gap: 4px; margin: 6px 0 2px; }
.why { margin: 4px 0 0; padding-left: 18px; font-size: var(--fs-sm); line-height: 1.6; }
.risk { font-size: var(--fs-xs); color: var(--red-ink); margin-top: 4px; }
.diff { background: var(--surface-2); border-radius: var(--radius-sm); padding: 8px 10px; font-size: var(--fs-sm); line-height: 1.6; margin-top: 10px; }
.sec { font-size: var(--fs-sm); font-weight: 600; margin: 12px 0 2px; }
.small { font-size: var(--fs-xs); }
.small .k { flex-shrink: 1; }
</style>
