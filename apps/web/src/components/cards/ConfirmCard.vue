<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { showToast } from 'vant'
import { api } from '../../api'

const props = defineProps({ card: { type: Object, required: true } })
const emit = defineEmits(['outcome', 'modify', 'select'])

// 订单卡（订花、订蛋糕）的「修改」：换商品或送达日期。选好后回到对话重新报价，出一张新的确认卡，这张由后端作废。
// 其他确认卡的「修改」仍是作废后请用户重新说。
const editOpen = ref(false)
const editSku = ref(props.card.edit?.sku || '')
const editDate = ref(props.card.edit?.date || '')
const WEEK = '日一二三四五六'
const dayLabel = (d) => { const t = new Date(`${d}T00:00:00`); return `${d.slice(5)} 周${WEEK[t.getDay()]}` }
const editChanged = computed(() => editSku.value !== props.card.edit?.sku || editDate.value !== props.card.edit?.date)

function submitEdit() {
  const item = props.card.edit.items.find((i) => i.sku === editSku.value)
  editOpen.value = false
  emit('select', { label: '修改订单', detail: `${item.name} · ${dayLabel(editDate.value)}送达`,
                   selection: { type: 'order_edit', action_id: props.card.action_id, sku: editSku.value, date: editDate.value } })
}

const busy = ref(false)
const done = ref('')
const left = ref(0)
const cool = ref(0) // 冷静期剩余秒数（反诈）：倒计时结束前确认按钮不可用，后端也会拒绝提前确认
const coolEnd = Date.now() + (props.card.cooling_off_s || 0) * 1000
// 有效期按服务端给的剩余秒数算：演示「时间快进」后服务端时钟和浏览器不一致
const deadline = props.card.expires_in_s != null ? Date.now() + props.card.expires_in_s * 1000 : new Date(props.card.expires_at)
let timer

const isRed = computed(() => props.card.level === 'RED')
const expired = computed(() => !done.value && left.value <= 0)
const countdown = computed(() => `${Math.floor(left.value / 60)}:${String(left.value % 60).padStart(2, '0')}`)

function tick() {
  left.value = Math.max(0, Math.round((deadline - Date.now()) / 1000))
  cool.value = Math.max(0, Math.ceil((coolEnd - Date.now()) / 1000))
}
onMounted(() => { tick(); timer = setInterval(tick, 1000) })
onUnmounted(() => clearInterval(timer))

async function act(kind) {
  if (kind === 'modify' && props.card.edit?.kind === 'order') {
    editOpen.value = true
    return
  }
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
  <div class="card confirm" :class="{ red: isRed }" data-testid="confirm-card">
    <div class="card-title">
      <span>{{ card.title }}</span>
      <van-tag :type="isRed ? 'danger' : 'warning'" size="medium">
        {{ isRed ? '🔴' : '🟡' }} {{ card.level_label }}
      </van-tag>
    </div>
    <div v-if="card.warning" class="warning"><van-icon name="warning" /> {{ card.warning }}</div>
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
    <div v-else-if="card.voided" class="done-mask">{{ typeof card.voided === 'string' ? card.voided : '已移出计划，这张确认卡作废，没有发生资金变动' }}</div>
    <div v-else-if="expired" class="done-mask">确认卡已过期，{{ card.amount ? '未发生资金变动' : '没有做任何变更' }}</div>
    <template v-else>
      <div class="btns">
        <van-button size="small" :disabled="busy" @click="act('cancel')">取消</van-button>
        <van-button size="small" :disabled="busy" @click="act('modify')">修改</van-button>
        <van-button size="small" type="danger" :loading="busy" :disabled="cool > 0" @click="act('confirm')">
          {{ cool > 0 ? `冷静期 ${cool}s` : '确认' }}
        </van-button>
      </div>
      <div class="meta">{{ countdown }} 内有效 · 确认内容与执行内容一致（digest {{ card.digest.slice(0, 8) }}）</div>
    </template>
    <van-popup v-if="card.edit" v-model:show="editOpen" position="bottom" round closeable teleport="body"
               :style="{ maxHeight: '80%' }">
      <div class="edit" data-testid="order-edit">
        <div class="edit-title">修改订单</div>
        <div class="edit-sec">商品</div>
        <van-radio-group v-model="editSku">
          <van-cell v-for="i in card.edit.items" :key="i.sku" :title="i.name" :value="`¥${i.price}`" clickable
                    @click="editSku = i.sku">
            <template #icon><van-radio :name="i.sku" class="radio" /></template>
          </van-cell>
        </van-radio-group>
        <div class="edit-sec">送达日期（上午 10:00–12:00）</div>
        <div class="dates">
          <span v-for="d in card.edit.dates" :key="d" class="date" :class="{ on: d === editDate }" @click="editDate = d">
            {{ dayLabel(d) }}
          </span>
        </div>
        <van-button type="danger" round block :disabled="!editChanged" @click="submitEdit">按新选择重新出确认卡</van-button>
        <div class="edit-note">价格以商户重新报价为准；原确认卡会作废，新卡同样要你确认后才付款。</div>
      </div>
    </van-popup>
  </div>
</template>

<style scoped>
.confirm { border-top: 3px solid var(--yellow); }
.confirm.red { border-top-color: var(--red); }
.amount { margin: 2px 0 8px; }
.reasons { background: var(--yellow-soft); border-radius: 8px; padding: 8px 10px; font-size: 13px; margin-top: 6px; line-height: 1.6; }
.red .reasons { background: var(--red-soft); }
.hint { color: var(--red); margin-top: 2px; }
.warning { background: var(--red); color: #fff; border-radius: 8px; padding: 8px 10px; font-size: 13px; line-height: 1.6; margin-bottom: 8px; }
.edit { padding: 16px 16px 24px; }
.edit-title { font-size: 16px; font-weight: 600; text-align: center; margin-bottom: 8px; }
.edit-sec { font-size: 13px; color: var(--muted); margin: 12px 0 6px; }
.radio { margin-right: 8px; }
.dates { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 14px; }
.date { font-size: 12px; padding: 4px 10px; border-radius: 12px; background: #f4f5f7; cursor: pointer; }
.date.on { background: var(--brand); color: #fff; }
.edit-note { font-size: 12px; color: var(--muted); margin-top: 8px; line-height: 1.6; }
</style>
