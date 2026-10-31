<script setup>
// AA 收款进度：每位参与人「已发送 / 已付清」。金额都由后端按收款单渲染。
import { ref, watch } from 'vue'
import { showToast } from 'vant'
import { api } from '../../api'

const props = defineProps({ card: { type: Object, required: true } })
const view = ref(props.card)
watch(() => props.card, (c) => { view.value = c })

const TAG = { self: 'default', sent: 'warning', paid: 'success' }

async function simulatePay(share) { // 演示模式：模拟对方付款
  try {
    view.value = (await api.aaPay(share.id)).card
  } catch (e) {
    showToast(e.message)
  }
}
</script>

<template>
  <div class="card aa">
    <div class="card-title">
      <span>AA 收款{{ view.title ? ` · ${view.title}` : '' }}</span>
      <van-tag :type="view.status === 'paid' ? 'success' : 'warning'" size="medium">{{ view.status_label }}</van-tag>
    </div>
    <div class="row"><span class="k">总额</span><span class="v">¥{{ view.total }}</span></div>
    <div v-for="s in view.shares" :key="s.id" class="row share">
      <span class="k">{{ s.name }}</span>
      <span class="v">
        ¥{{ s.amount }}
        <van-tag plain :type="TAG[s.status]">{{ s.status_label }}</van-tag>
        <van-button v-if="view.demo && s.status === 'sent'" size="mini" plain @click="simulatePay(s)">模拟付款</van-button>
      </span>
    </div>
    <div class="row total"><span class="k">已收 / 应收</span><span class="v">¥{{ view.received }} / ¥{{ view.expected }}</span></div>
    <div class="meta">收款单 {{ view.bill_id }} · 发起于 {{ view.created_at }}</div>
  </div>
</template>

<style scoped>
.share .v { display: flex; align-items: center; gap: 6px; justify-content: flex-end; }
.total { border-top: 1px dashed var(--line); margin-top: 4px; padding-top: 8px; }
</style>
