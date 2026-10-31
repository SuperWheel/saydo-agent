<script setup>
// 交易查询（F-BA-02）：卡片上只列前几笔，点卡片打开完整明细（按日期分组、滚动加载），合计和卡片一致。
import { computed, ref } from 'vue'
import BillTxnSheet from './BillTxnSheet.vue'

const props = defineProps({ card: { type: Object, required: true } })
const open = ref(false)
const hasDetail = computed(() => !!props.card.detail && props.card.count > 0)
</script>

<template>
  <div class="card" :class="{ tap: hasDetail }" data-testid="txn-list-card" @click="open = hasDetail">
    <div class="card-title"><span>{{ card.title }}</span><van-tag plain type="success"><IconText text="🟢 自动查询" /></van-tag></div>
    <div class="head">
      <div class="period">{{ card.period }}<span v-for="f in card.filters" :key="f" class="chip">{{ f }}</span></div>
      <div v-if="card.single" class="sum"><span class="k">符合条件的{{ card.kind }} {{ card.count }} 笔，{{ card.single }}的一笔：</span></div>
      <div v-else class="sum"><span class="k">{{ card.kind }} {{ card.count }} 笔，合计</span><span class="money">¥{{ card.total }}</span></div>
    </div>
    <div v-for="(t, i) in card.items" :key="i" class="txn">
      <div class="l">
        <div class="name">{{ t.name }}</div>
        <div class="sub">{{ t.time }} · {{ t.category }}<span v-for="g in t.tags" :key="g" class="tag" :class="{ warn: g === '境外' }">{{ g }}</span></div>
      </div>
      <div class="amt" :class="t.direction">{{ t.amount }}</div>
    </div>
    <div v-if="!card.items.length" class="empty">没有符合条件的交易</div>
    <div v-if="hasDetail" class="more">
      <span v-if="card.more && !card.single">另有 {{ card.more }} 笔未列出 · </span>查看全部 {{ card.count }} 笔明细 ›
    </div>
    <div class="meta">数据来源：{{ card.source }} · {{ card.as_of }}</div>
    <van-popup v-model:show="open" position="bottom" round closeable :style="{ height: '86%' }" teleport="body">
      <BillTxnSheet v-if="open" :query="card.detail" :period="card.period" :title="`${card.kind}明细`" />
    </van-popup>
  </div>
</template>

<style scoped>
.tap { cursor: pointer; }
.more { font-size: var(--fs-xs); color: var(--brand); text-align: right; margin-top: 6px; }
.head { border-bottom: 1px dashed var(--line); padding-bottom: 8px; margin-bottom: 4px; }
.period { font-size: var(--fs-sm); color: var(--muted); display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.chip { background: var(--surface-2); border-radius: var(--radius-xs); padding: 0 6px; font-size: var(--fs-xs); }
.sum { display: flex; justify-content: space-between; align-items: baseline; margin-top: 4px; }
.sum .k { color: var(--muted); font-size: var(--fs-sm); }
.sum .money { font-size: var(--fs-money-md); }
.txn { display: flex; justify-content: space-between; gap: 10px; padding: 7px 0; border-bottom: 1px solid var(--line-soft); }
.txn:last-of-type { border-bottom: 0; }
.l { min-width: 0; }
.name { font-size: var(--fs-body); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sub { font-size: var(--fs-xs); color: var(--muted); margin-top: 2px; }
.tag { margin-left: 4px; padding: 0 4px; border-radius: var(--radius-xs); background: var(--surface-2); }
.tag.warn { background: var(--yellow-soft); color: var(--yellow-ink); }
.amt { font-variant-numeric: tabular-nums; font-weight: 600; white-space: nowrap; }
.amt.in { color: var(--green-ink); }
.empty { color: var(--muted); font-size: var(--fs-sm); text-align: center; padding: 10px 0; }
</style>
