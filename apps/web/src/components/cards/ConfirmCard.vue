<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { showToast } from 'vant'
import { api } from '../../api'

const props = defineProps({ card: { type: Object, required: true } })
const emit = defineEmits(['outcome', 'modify'])

const busy = ref(false)
const done = ref('')
const left = ref(0)
let timer

const isRed = computed(() => props.card.level === 'RED')
const expired = computed(() => !done.value && left.value <= 0)
const countdown = computed(() => `${Math.floor(left.value / 60)}:${String(left.value % 60).padStart(2, '0')}`)

function tick() {
  left.value = Math.max(0, Math.round((new Date(props.card.expires_at) - Date.now()) / 1000))
}
onMounted(() => { tick(); timer = setInterval(tick, 1000) })
onUnmounted(() => clearInterval(timer))

async function act(kind) {
  busy.value = true
  try {
    const out = kind === 'confirm'
      ? await api.confirm(props.card.action_id, props.card.digest)
      : await api.cancel(props.card.action_id)
    done.value = { confirm: '已提交确认', cancel: '已取消', modify: '已作废，请重新说一遍' }[kind]
    clearInterval(timer)
    emit('outcome', out)
    if (kind === 'modify') emit('modify')
  } catch (e) {
    showToast(e.message)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="card confirm" :class="{ red: isRed }">
    <div class="card-title">
      <span>{{ card.title }}</span>
      <van-tag :type="isRed ? 'danger' : 'warning'" size="medium">
        {{ isRed ? '🔴' : '🟡' }} {{ card.level_label }}
      </van-tag>
    </div>
    <div v-if="card.amount" class="amount">
      <span class="money">¥{{ card.amount }}</span>
      <div class="capital">{{ card.amount_capital }}</div>
    </div>
    <div v-for="[k, v] in card.rows" :key="k" class="row"><span class="k">{{ k }}</span><span class="v">{{ v }}</span></div>
    <div v-if="card.today_after" class="row"><span class="k">今日累计转出</span><span class="v">¥{{ card.today_after }}</span></div>
    <div class="reasons">
      <div v-for="r in card.reasons" :key="r">· {{ r }}</div>
      <div v-if="card.auth_hint" class="hint">{{ card.auth_hint }}</div>
    </div>
    <div v-if="done" class="done-mask">{{ done }}</div>
    <div v-else-if="expired" class="done-mask">确认卡已过期，{{ card.amount ? '未发生资金变动' : '没有做任何变更' }}</div>
    <template v-else>
      <div class="btns">
        <van-button size="small" :disabled="busy" @click="act('cancel')">取消</van-button>
        <van-button size="small" :disabled="busy" @click="act('modify')">修改</van-button>
        <van-button size="small" type="danger" :loading="busy" @click="act('confirm')">确认</van-button>
      </div>
      <div class="meta">{{ countdown }} 内有效 · 确认内容与执行内容一致（digest {{ card.digest.slice(0, 8) }}）</div>
    </template>
  </div>
</template>

<style scoped>
.confirm { border-top: 3px solid var(--yellow); }
.confirm.red { border-top-color: var(--red); }
.amount { margin: 2px 0 8px; }
.reasons { background: var(--yellow-soft); border-radius: 8px; padding: 8px 10px; font-size: 13px; margin-top: 6px; line-height: 1.6; }
.red .reasons { background: var(--red-soft); }
.hint { color: var(--red); margin-top: 2px; }
</style>
