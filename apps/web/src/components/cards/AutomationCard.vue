<script setup>
// 自动任务总览（WS7）：心愿罐、定时转账、进行中的计划、花店蛋糕店订单。金额都由后端渲染。
defineProps({ card: { type: Object, required: true } })
</script>

<template>
  <div class="card automation">
    <div class="card-title"><span>我的心愿罐和自动任务</span></div>
    <template v-if="card.pots.length">
      <div class="sec">心愿罐</div>
      <div v-for="p in card.pots" :key="p.pot_id" class="row">
        <span class="k">「{{ p.purpose }}」{{ p.until ? ` · ${p.until} 到期` : '' }}</span><span class="v">¥{{ p.balance }}</span>
      </div>
    </template>
    <template v-if="card.schedules.length">
      <div class="sec">定时转账</div>
      <div v-for="s in card.schedules" :key="s.schedule_id" class="row">
        <span class="k">{{ s.label }}</span><span class="v">下次 {{ s.next_run }}</span>
      </div>
    </template>
    <template v-if="card.plans.length">
      <div class="sec">进行中的计划</div>
      <div v-for="p in card.plans" :key="p.plan_id" class="row">
        <span class="k">{{ p.goal }}</span><span class="v">{{ p.status }}{{ p.due_at ? ` · ${p.due_at}` : '' }}</span>
      </div>
    </template>
    <template v-if="card.orders.length">
      <div class="sec">订单</div>
      <div v-for="o in card.orders" :key="o.order_id" class="row">
        <span class="k">{{ o.item }} · {{ o.delivery }}</span><span class="v">¥{{ o.amount }}</span>
      </div>
    </template>
    <div v-if="!card.pots.length && !card.schedules.length && !card.plans.length && !card.orders.length" class="meta">
      暂时没有心愿罐或自动任务
    </div>
  </div>
</template>

<style scoped>
.automation { border-top: 3px solid var(--brand); }
.sec { font-size: 12px; color: var(--muted); margin-top: 8px; border-bottom: 1px solid var(--line); padding-bottom: 2px; }
</style>
