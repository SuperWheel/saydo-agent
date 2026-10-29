<script setup>
defineProps({ card: { type: Object, required: true } })
</script>

<template>
  <div class="card">
    <div class="card-title"><span>{{ card.title }}</span><van-tag plain type="success">🟢 自动查询</van-tag></div>
    <div class="period">{{ card.period }} · {{ card.count ? `${card.count} 笔需要留意` : '没有发现异常消费' }}</div>
    <div v-for="(a, i) in card.items" :key="i" class="item" :class="{ high: a.level === '需要核实' }">
      <div class="top">
        <span class="name">{{ a.name }}</span>
        <span class="amt">¥{{ a.amount }}<small v-if="a.count > 1"> × {{ a.count }}</small></span>
      </div>
      <div class="sub">
        <van-tag :type="a.level === '需要核实' ? 'danger' : 'warning'" plain>{{ a.level }}</van-tag>
        <span class="time">{{ a.time }}</span>
        <span v-for="s in a.signals" :key="s" class="sig">{{ s }}</span>
      </div>
      <div class="reason">{{ a.reason }}</div>
      <div class="advice">{{ a.advice }}</div>
    </div>
    <div v-if="!card.count" class="ok"><van-icon name="passed" /> 一切正常</div>
    <div class="meta">按个人近 90 天消费习惯判断，仅供参考，不代表已确认被盗刷 · {{ card.source }}</div>
  </div>
</template>

<style scoped>
.period { font-size: 13px; color: var(--muted); margin-bottom: 4px; }
.item { border-left: 3px solid var(--yellow); background: var(--yellow-soft); border-radius: 6px; padding: 8px 10px; margin-top: 8px; }
.item.high { border-left-color: var(--red); background: var(--red-soft); }
.top { display: flex; justify-content: space-between; gap: 8px; font-weight: 600; font-size: 14px; }
.name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.amt { font-variant-numeric: tabular-nums; white-space: nowrap; }
.amt small { font-weight: 400; color: var(--muted); }
.sub { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; margin-top: 4px; font-size: 12px; color: var(--muted); }
.sig { background: rgba(255, 255, 255, 0.7); border-radius: 3px; padding: 0 4px; }
.reason { font-size: 13px; line-height: 1.5; margin-top: 6px; }
.advice { font-size: 12px; color: var(--muted); margin-top: 4px; }
.ok { color: var(--green); font-size: 14px; text-align: center; padding: 8px 0; }
</style>
