<script setup>
import { computed, ref } from 'vue'
import { showToast } from 'vant'
import { api } from '../../api'
import { icon } from '../../icons'

const props = defineProps({ card: { type: Object, required: true } })
const emit = defineEmits(['outcome'])

const cur = ref(props.card)
const code = ref('')
const busy = ref('')
const done = ref(false)
const faceDone = computed(() => cur.value.factors.find((f) => f.factor === 'face')?.passed)

async function verify(factor) {
  busy.value = factor
  try {
    const value = factor === 'face' ? `face-ok:${cur.value.digest}` : code.value
    const out = await api.verify(cur.value.challenge_id, factor, value)
    if (out.status === 'awaiting_auth') {
      cur.value = out.card
      if (out.message) showToast(out.message)
      if (factor === 'sms') code.value = ''
    } else {
      if (out.status === 'succeeded') {
        cur.value = { ...cur.value, factors: cur.value.factors.map((f) => ({ ...f, passed: true })) }
      }
      done.value = true
      emit('outcome', out)
    }
  } catch (e) {
    showToast(e.message)
  } finally {
    busy.value = ''
  }
}
</script>

<template>
  <div class="card auth" :class="{ closed: done }" data-testid="auth-card">
    <div class="card-title">
      <span>强验证（模拟）</span>
      <van-tag type="danger" size="medium"><IconText text="🔴 两步验证" /></van-tag>
    </div>
    <div class="summary">{{ cur.summary }}</div>
    <div v-for="f in cur.factors" :key="f.factor" class="row step">
      <span class="k">{{ f.label }}</span>
      <span class="v" :class="{ ok: f.passed }">{{ f.passed ? '已通过' : '待完成' }}</span>
    </div>
    <template v-if="!done">
      <van-button v-if="!faceDone" block size="small" type="danger" plain :loading="busy === 'face'"
                  :icon="icon('face-id', 'contact')" @click="verify('face')">模拟人脸识别</van-button>
      <template v-else>
        <div v-if="cur.sms_preview" class="sms">
          <div class="sms-label">模拟短信（演示环境直接显示）</div>
          {{ cur.sms_preview }}
        </div>
        <div class="sms-input">
          <van-field v-model="code" type="digit" maxlength="6" placeholder="输入 6 位验证码" />
          <van-button size="small" type="danger" :loading="busy === 'sms'" :disabled="code.length !== 6"
                      @click="verify('sms')">提交</van-button>
        </div>
      </template>
      <div class="meta">还可尝试 {{ cur.attempts_left }} 次 · 人脸不能作为唯一验证方式 · 验证码绑定本笔交易摘要</div>
    </template>
    <div v-else class="done-mask">验证流程已结束</div>
  </div>
</template>

<style scoped>
.summary { font-size: var(--fs-body); line-height: 1.55; margin-bottom: 6px; }
.step .ok { color: var(--green-ink); font-weight: 600; }
.auth .van-button--block { margin-top: 10px; }
.sms { background: var(--surface-2); border-radius: var(--radius-sm); padding: 10px var(--space-3); font-size: var(--fs-sm); line-height: 1.6; margin-top: 10px; }
.sms-label { color: var(--muted); font-size: var(--fs-2xs); margin-bottom: var(--space-1); }
.sms-input { display: flex; align-items: center; gap: var(--space-2); margin-top: var(--space-2); }
.sms-input .van-field { flex: 1; background: var(--surface-2); border-radius: var(--radius-sm); padding: 6px 10px; }
</style>
