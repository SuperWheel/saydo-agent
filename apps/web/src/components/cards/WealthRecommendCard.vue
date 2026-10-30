<script setup>
// 理财推荐（F-WM-02）：只列风险等级不高于客户等级的产品。分数、收益参考、过滤原因都来自后端，这里只负责排版。
defineProps({ card: { type: Object, required: true } })
</script>

<template>
  <div class="card rec">
    <div class="card-title"><span>为你筛选的理财产品</span><van-tag plain type="success">🟢 自动查询</van-tag></div>
    <div class="cond">
      风险承受能力 <b>{{ card.profile.level }}</b> · 可购 {{ card.profile.range }}<span v-if="card.condition"> · {{ card.condition }}</span>
    </div>
    <div v-for="(p, i) in card.items" :key="p.code" class="item">
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
    </div>
    <div v-if="card.diff" class="diff">{{ card.diff }}</div>
    <template v-if="card.filtered.length">
      <div class="sec">没有推荐的产品</div>
      <div v-for="f in card.filtered" :key="f.name" class="row small"><span class="k">{{ f.name }}</span><span class="v">{{ f.reason }}</span></div>
    </template>
    <div class="meta">打分权重：{{ card.weights }}</div>
    <div class="meta">{{ card.disclaimer }}</div>
    <div class="meta">数据来源：{{ card.source }} · {{ card.as_of }}</div>
  </div>
</template>

<style scoped>
.rec { border-top: 3px solid var(--brand); }
.cond { font-size: 13px; color: var(--muted); margin-bottom: 8px; }
.cond b { color: var(--text); }
.item { border: 1px solid var(--line); border-radius: 10px; padding: 10px; }
.item + .item { margin-top: 8px; }
.top { display: flex; justify-content: space-between; align-items: center; font-weight: 600; font-size: 14px; }
.score { color: var(--brand); font-variant-numeric: tabular-nums; }
.rank { display: inline-block; width: 16px; height: 16px; line-height: 16px; text-align: center; font-style: normal;
  font-size: 11px; border-radius: 50%; background: var(--brand); color: #fff; margin-right: 6px; }
.tags { display: flex; flex-wrap: wrap; gap: 4px; margin: 6px 0 2px; }
.why { margin: 4px 0 0; padding-left: 18px; font-size: 13px; line-height: 1.6; }
.risk { font-size: 12px; color: var(--red); margin-top: 4px; }
.diff { background: #f6f7fa; border-radius: 8px; padding: 8px 10px; font-size: 13px; line-height: 1.6; margin-top: 10px; }
.sec { font-size: 13px; font-weight: 600; margin: 12px 0 2px; }
.small { font-size: 12px; }
.small .k { flex-shrink: 1; }
</style>
