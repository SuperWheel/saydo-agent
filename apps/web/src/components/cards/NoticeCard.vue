<script setup>
// 普通提示，以及熔断提示（card.breaker）：熔断时要向用户说明原因类别、恢复方式和申诉渠道。
defineProps({ card: { type: Object, required: true } })
</script>

<template>
  <div v-if="card.breaker" class="card notice breaker" data-testid="breaker-card">
    <div class="b-head">
      <van-icon name="shield-o" size="18" />
      <span>安全保护已开启</span>
      <van-tag type="danger" plain>{{ card.breaker.level_label }}</van-tag>
    </div>
    <div class="row"><span class="k">原因</span><span class="v">{{ card.breaker.reason }}</span></div>
    <div class="row"><span class="k">影响</span><span class="v">{{ card.breaker.affects }}</span></div>
    <div class="row"><span class="k">恢复</span><span class="v">{{ card.breaker.recover }}</span></div>
    <div class="ok">✓ {{ card.breaker.still_available }}</div>
    <div class="appeal">{{ card.breaker.appeal }}</div>
  </div>
  <div v-else class="card notice" :class="card.level">
    <van-icon :name="card.level === 'error' ? 'close' : card.level === 'warning' ? 'warning-o' : 'info-o'" />
    <span>{{ card.text }}</span>
  </div>
</template>

<style scoped>
.notice { display: flex; gap: 8px; align-items: flex-start; font-size: 14px; line-height: 1.5; background: #f4f6fa; }
.notice.warning { background: var(--yellow-soft); color: #874d00; }
.notice.error { background: var(--red-soft); color: var(--red); }
.breaker { display: block; border-top: 3px solid var(--red); background: var(--surface); }
.b-head { display: flex; align-items: center; gap: 6px; font-weight: 600; color: var(--red); margin-bottom: 6px; }
.ok { font-size: 13px; color: var(--green); margin-top: 6px; }
.appeal { font-size: 12px; margin-top: 8px; padding: 8px; border-radius: 8px; background: var(--yellow-soft); color: #874d00; line-height: 1.5; }
</style>
