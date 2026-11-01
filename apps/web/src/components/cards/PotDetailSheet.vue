<script setup>
// 心愿罐明细弹层（自动任务卡里点心愿罐那一行打开）：GET /api/pots/{pot_id}/txns，余额和收支明细按时间倒序。
import { ref, watch } from 'vue'
import { api } from '../../api'
import PotTxnRows from './PotTxnRows.vue'

const props = defineProps({ potId: { type: [String, Number], default: '' }, purpose: { type: String, default: '' } })

const d = ref(null)
const err = ref('')

watch(() => props.potId, async (id) => {
  d.value = null
  err.value = ''
  if (!id) return
  try { d.value = await api.potTxns(id) } catch (e) { err.value = e.message }
}, { immediate: true })
</script>

<template>
  <div class="sheet-frame" data-testid="pot-detail-sheet">
    <div class="sheet-head" data-testid="sheet-head">
      <div class="sheet-title">心愿罐「{{ d?.pot.purpose || purpose }}」</div>
      <div v-if="d" class="sheet-sum"><span class="k">当前余额</span><b class="money">¥{{ d.pot.balance }}</b></div>
    </div>
    <div class="sheet-body">
      <div v-if="err" class="err">{{ err }}</div>
      <van-loading v-else-if="!d" class="loading" />
      <template v-else>
        <PotTxnRows :items="d.items" />
        <div class="meta">存入不用验证；从心愿罐付款只限合作商户，每笔仍要你确认</div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.err { color: var(--red); padding: 24px 0; text-align: center; }
.loading { display: flex; justify-content: center; padding: 32px; }
</style>
