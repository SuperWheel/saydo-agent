<script setup>
defineProps({ card: { type: Object, required: true } })
</script>

<template>
  <div class="card">
    <div class="card-title"><span>{{ card.title }}</span><van-tag plain type="success">🟢 自动查询</van-tag></div>
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
    <div v-if="card.more && !card.single" class="meta">另有 {{ card.more }} 笔未列出</div>
    <div class="meta">数据来源：{{ card.source }} · {{ card.as_of }}</div>
  </div>
</template>

<style scoped>
.head { border-bottom: 1px dashed var(--line); padding-bottom: 8px; margin-bottom: 4px; }
.period { font-size: 13px; color: var(--muted); display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.chip { background: #f4f6fa; border-radius: 4px; padding: 0 6px; font-size: 12px; }
.sum { display: flex; justify-content: space-between; align-items: baseline; margin-top: 4px; }
.sum .k { color: var(--muted); font-size: 13px; }
.sum .money { font-size: 20px; }
.txn { display: flex; justify-content: space-between; gap: 10px; padding: 7px 0; border-bottom: 1px solid #f3f4f7; }
.txn:last-of-type { border-bottom: 0; }
.l { min-width: 0; }
.name { font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sub { font-size: 12px; color: var(--muted); margin-top: 2px; }
.tag { margin-left: 4px; padding: 0 4px; border-radius: 3px; background: #f4f6fa; }
.tag.warn { background: var(--yellow-soft); color: var(--yellow); }
.amt { font-variant-numeric: tabular-nums; font-weight: 600; white-space: nowrap; }
.amt.in { color: var(--green); }
.empty { color: var(--muted); font-size: 13px; text-align: center; padding: 10px 0; }
</style>
