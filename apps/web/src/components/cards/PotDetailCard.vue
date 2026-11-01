<script setup>
// 心愿罐明细（对话里说「心愿罐明细」）：余额和收支明细，按时间倒序。较多时先显示前几笔，可展开。
import { computed, ref } from 'vue'
import PotTxnRows from './PotTxnRows.vue'

const props = defineProps({ card: { type: Object, required: true } })
const FOLD = 8
const all = ref(false)
const list = computed(() => (all.value ? props.card.items : props.card.items.slice(0, FOLD)))
</script>

<template>
  <div class="card pot" data-testid="pot-detail-card">
    <div class="card-title"><span>心愿罐「{{ card.pot.purpose }}」</span><van-tag plain type="success"><IconText text="🟢 自动查询" /></van-tag></div>
    <div class="bal"><span class="k">当前余额</span><span class="money">¥{{ card.pot.balance }}</span></div>
    <PotTxnRows :items="list" />
    <div v-if="card.items.length > FOLD" class="toggle" @click="all = !all">{{ all ? '收起' : `展开全部 ${card.items.length} 笔` }}</div>
    <div class="meta">转入需人脸或短信验证（任选其一），转出到活期只需确认；罐里的钱只能在合作商户消费</div>
  </div>
</template>

<style scoped>
.bal { display: flex; justify-content: space-between; align-items: baseline; padding-bottom: 6px; border-bottom: 1px solid var(--line); }
.bal .k { color: var(--muted); font-size: var(--fs-sm); }
.bal .money { font-size: var(--fs-money-md); line-height: 28px; }
.toggle { text-align: center; font-size: var(--fs-sm); color: var(--info); padding: 8px 0 0; cursor: pointer; }
</style>
