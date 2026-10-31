<script setup>
// 银行卡详情（卡片列表上点一张卡打开）：开户行、卡种、户名、状态、余额或额度、限额，全部来自服务端。
// 卡号默认只显示前 4 后 4。点小眼睛先做模拟人脸验证，通过后服务端才返回完整卡号；本次打开期间可以来回显示 / 隐藏，
// 关掉详情就清掉，再看要重新验证。完整卡号只放在这个组件的内存里，不进对话、不进本地存储。
// 绑定进来的卡底部有「解绑此卡」：发出 unbind，由卡片列表转成 selection 回到对话，出解绑确认卡（和说「解绑尾号1554的卡」同一条路）。
import { ref, watch } from 'vue'
import { showToast } from 'vant'
import { api } from '../../api'
import { icon } from '../../icons'

const props = defineProps({ cardId: { type: Number, default: 0 } })
const emit = defineEmits(['close', 'unbind'])

const d = ref(null)
const err = ref('')
const fullNo = ref('') // 人脸通过后拿到的完整卡号
const shown = ref(false)
const challenge = ref(null) // 进行中的人脸验证
const busy = ref(false)

function reset() {
  d.value = null
  err.value = ''
  fullNo.value = ''
  shown.value = false
  challenge.value = null
}

watch(() => props.cardId, async (id) => {
  reset()
  if (!id) return
  try { d.value = await api.cardDetail(id) } catch (e) { err.value = e.message }
}, { immediate: true })

async function toggleEye() {
  if (fullNo.value) { shown.value = !shown.value; return }
  if (challenge.value || busy.value) return
  busy.value = true
  try { challenge.value = await api.cardReveal(props.cardId) } catch (e) { showToast(e.message) } finally { busy.value = false }
}

async function verifyFace() {
  busy.value = true
  try {
    const out = await api.cardRevealVerify(challenge.value.challenge_id, `face-ok:${challenge.value.digest}`)
    fullNo.value = out.card_no
    shown.value = true
    challenge.value = null
  } catch (e) {
    showToast(e.message)
    if (e.status !== 403 || /过多/.test(e.message)) challenge.value = null // 失败次数用完、超时：要重新点小眼睛
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="detail" data-testid="card-detail">
    <div class="bar" data-testid="card-detail-bar">
      <span class="title">{{ d ? d.name : '银行卡详情' }}</span>
      <van-icon name="cross" class="close" aria-label="关闭" @click="emit('close')" />
    </div>
    <div class="body">
      <div v-if="err" class="err">{{ err }}</div>
      <van-loading v-else-if="!d" class="loading" />
      <template v-else>
        <div class="face" :class="[d.kind, d.status]">
          <div class="face-top">
            <span>{{ d.bank }}</span>
            <van-tag :type="{ active: 'success', locked: 'warning', lost: 'danger' }[d.status]" size="medium">{{ d.status_label }}</van-tag>
          </div>
          <div class="no-line">
            <span class="no" data-testid="card-number">{{ shown ? fullNo : d.card_no_masked }}</span>
            <button class="eye" type="button" :aria-label="shown ? '隐藏完整卡号' : '显示完整卡号'"
                    data-testid="card-eye" :disabled="busy" @click="toggleEye">
              <van-icon :name="shown ? 'eye-o' : 'closed-eye'" />
            </button>
          </div>
          <div class="kind">{{ d.kind_label }}{{ d.bound ? ' · 他行卡' : '' }}</div>
        </div>

        <div v-if="challenge" class="verify" data-testid="card-face">
          <div class="v-title"><van-icon :name="icon('face-id', 'contact')" /> 本人验证（模拟）</div>
          <div class="v-text">{{ challenge.summary }}，需要人脸识别。只用人脸，不发短信验证码。</div>
          <div class="btns">
            <van-button size="small" plain @click="challenge = null">取消</van-button>
            <van-button size="small" type="danger" :loading="busy" :icon="icon('face-id', 'contact')"
                        data-testid="card-face-btn" @click="verifyFace">模拟人脸识别</van-button>
          </div>
        </div>

        <div class="money-grid">
          <div v-for="[k, v] in d.money" :key="k" class="m"><span>{{ k }}</span><b>{{ v }}</b></div>
        </div>

        <div class="sec">卡片信息</div>
        <div v-for="[k, v] in d.rows" :key="k" class="row"><span class="k">{{ k }}</span><span class="v">{{ v }}</span></div>

        <div class="sec">交易限制</div>
        <div v-for="[k, v] in d.limits" :key="k" class="row">
          <span class="k">{{ k }}</span><span class="v" :class="{ off: v === '已关闭' }">{{ v }}</span>
        </div>

        <div v-if="d.unbind" class="unbind">
          <van-button v-if="d.unbind.allowed" block plain type="danger" size="small" data-testid="card-unbind"
                      @click="emit('unbind', d)">解绑此卡</van-button>
          <div v-else class="unbind-why" data-testid="card-unbind-why">{{ d.unbind.reason }}</div>
        </div>
        <div class="meta">{{ d.reveal.note }} · 锁卡、挂失、调整限额直接在对话里说</div>
        <div class="meta">数据来源：{{ d.source }} · {{ d.as_of }}</div>
      </template>
    </div>
  </div>
</template>

<style scoped>
/* 弹层整体是一列：顶栏固定，内容区自己滚动，滚动时顶栏不动 */
.detail { display: flex; flex-direction: column; max-height: 86vh; }
.bar {
  position: sticky; top: 0; z-index: 1; flex-shrink: 0;
  display: flex; align-items: center; justify-content: space-between; gap: var(--space-2);
  padding: 14px var(--space-4) 10px; background: var(--surface); border-bottom: 1px solid var(--line-soft);
}
.title { font-size: var(--fs-sheet); font-weight: 600; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.close { font-size: 20px; color: var(--muted); cursor: pointer; padding: 2px; }
.body { flex: 1; overflow-y: auto; padding: var(--space-3) var(--space-4) var(--space-6); -webkit-overflow-scrolling: touch; }
.loading { display: flex; justify-content: center; padding: 32px; }
.err { color: var(--red); padding: 24px 0; text-align: center; }

.face { border-radius: var(--radius-md); padding: 12px 14px; color: var(--on-brand); background: linear-gradient(135deg, #c8102e, #8a0b20); }
.face.credit { background: linear-gradient(135deg, #3a3f4b, #1d2330); }
.face.virtual { background: linear-gradient(135deg, #3b6fd4, #23468f); }
.face.locked, .face.lost { background: linear-gradient(135deg, #8d929c, #5f646e); }
.face-top { display: flex; justify-content: space-between; align-items: center; font-weight: 600; font-size: var(--fs-body); }
.no-line { display: flex; align-items: center; gap: var(--space-2); margin: 14px 0 4px; }
.no { font-size: var(--fs-sheet); letter-spacing: 1px; font-variant-numeric: tabular-nums; font-family: var(--font-mono); word-break: break-all; }
.eye { flex-shrink: 0; display: inline-flex; align-items: center; justify-content: center; width: 30px; height: 30px; border: 0;
  border-radius: var(--radius-pill); background: rgba(255, 255, 255, 0.18); color: var(--on-brand); font-size: 18px; cursor: pointer; }
.eye:disabled { opacity: 0.6; }
.kind { font-size: var(--fs-xs); opacity: 0.8; }

.verify { margin-top: var(--space-3); padding: 10px var(--space-3); border-radius: var(--radius-sm); background: var(--red-soft); }
.v-title { display: flex; align-items: center; gap: 6px; font-weight: 600; font-size: var(--fs-body); color: var(--red-ink); }
.v-title :deep(.van-icon__image) { width: 16px; height: 16px; }
.v-text { font-size: var(--fs-sm); line-height: 1.6; margin-top: 4px; }
.verify .btns { margin-top: 10px; }

.money-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: var(--space-2); margin-top: var(--space-3); }
.m { background: var(--surface-2); border-radius: var(--radius-sm); padding: 8px 10px; font-size: var(--fs-xs); color: var(--muted); }
.m b { display: block; margin-top: 2px; font-size: var(--fs-money-md); color: var(--ink); font-variant-numeric: tabular-nums; }
.sec { font-size: var(--fs-sm); font-weight: 600; margin: 16px 0 4px; }
.row .v.off { color: var(--muted); }
.unbind { margin-top: var(--space-4); }
.unbind-why { font-size: var(--fs-xs); color: var(--muted); line-height: 1.6; background: var(--surface-2); border-radius: var(--radius-sm); padding: 8px 10px; }
</style>
