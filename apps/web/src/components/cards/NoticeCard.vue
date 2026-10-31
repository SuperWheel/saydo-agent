<script setup>
// 普通提示，以及熔断提示（card.breaker）：熔断时要向用户说明原因类别、恢复方式和申诉渠道。
// 有处置记录的（card.breaker.support）可以点「联系人工客服」，由模拟客服核实后解除（SupportSheet）。
import { ref } from 'vue'
import SupportSheet from './SupportSheet.vue'
import { icon, iconUrl } from '../../icons'

const custom = !!iconUrl('shield-alert') // 有定制盾牌图标时不再画圆形底

const props = defineProps({ card: { type: Object, required: true } })
const emit = defineEmits(['outcome', 'say'])

const open = ref(false)
const released = ref(false)

function onReleased(out) {
  released.value = true
  open.value = false
  emit('outcome', { status: 'released', message: out.message, cards: [] })
}

function onSay(text) {
  open.value = false
  emit('say', text)
}
</script>

<template>
  <div v-if="card.breaker" class="card notice breaker" :class="{ released }" data-testid="breaker-card">
    <div class="status-head">
      <span class="status-icon" :class="{ ok: released, img: custom }">
        <van-icon :name="released ? icon('shield-check', 'passed') : icon('shield-alert', 'shield-o')" :size="custom ? 36 : 20" />
      </span>
      <span class="title">安全保护已开启</span>
      <span class="tags">
        <van-tag type="danger" plain>{{ card.breaker.level_label }}</van-tag>
        <van-tag v-if="released" type="success" data-testid="breaker-released">已由人工客服解除</van-tag>
      </span>
    </div>
    <div class="row"><span class="k">原因</span><span class="v">{{ card.breaker.reason }}</span></div>
    <div class="row"><span class="k">影响</span><span class="v">{{ card.breaker.affects }}</span></div>
    <div class="row"><span class="k">恢复</span><span class="v">{{ card.breaker.recover }}</span></div>
    <div class="ok"><van-icon :name="icon('risk-green', 'success')" /> {{ card.breaker.still_available }}</div>
    <div class="appeal">{{ card.breaker.appeal }}</div>
    <van-button v-if="card.breaker.support && !released" block round size="small" type="danger" plain :icon="icon('support', 'service-o')"
                class="support" data-testid="breaker-support" @click="open = true">联系人工客服</van-button>

    <van-popup v-model:show="open" position="bottom" round closeable :style="{ height: '86%' }" teleport="body">
      <SupportSheet v-if="open" :trip-id="card.breaker.trip_id" @released="onReleased" @say="onSay" @close="open = false" />
    </van-popup>
  </div>
  <div v-else class="card notice" :class="card.level">
    <van-icon :name="card.level === 'error' ? 'close' : card.level === 'warning' ? icon('warning', 'warning-o') : 'info-o'" />
    <span>{{ card.text }}</span>
  </div>
</template>

<style scoped>
.notice { display: flex; gap: var(--space-2); align-items: flex-start; font-size: var(--fs-body); line-height: 1.55; background: var(--surface-2); box-shadow: none; }
.notice > .van-icon { margin-top: 3px; flex-shrink: 0; }
.notice.warning { background: var(--yellow-soft); color: var(--yellow-ink); }
.notice.error { background: var(--red-soft); color: var(--red-ink); }
.breaker { display: block; background: var(--surface); box-shadow: var(--shadow-card); }
.breaker .row { padding: 4px 0; }
.breaker .row .k { width: 40px; }
.breaker .row .v { flex: 1; text-align: left; }
.breaker .status-head + .row { margin-top: var(--space-2); }
.ok { display: flex; align-items: center; gap: var(--space-1); font-size: var(--fs-sm); color: var(--green-ink); margin-top: var(--space-2); }
.appeal { font-size: var(--fs-xs); margin-top: 10px; padding: var(--space-2) var(--space-3); border-radius: var(--radius-sm); background: var(--surface-2); color: var(--muted); line-height: 1.6; }
.tags { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: var(--space-1); max-width: 50%; }
.support { margin-top: var(--space-3); border-radius: var(--radius-sm); }
</style>
