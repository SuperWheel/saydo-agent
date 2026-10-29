<script setup>
// 运营后台 · 熔断与风险（技术文档 §5.7）：当前处置（可人工解除）、工具断路器、最近的安全事件、阈值配置、全局只读开关。
import { computed, onMounted, ref } from 'vue'
import { showConfirmDialog, showToast } from 'vant'
import { api } from '../api'

const emit = defineEmits(['replay'])

const data = ref(null)
const loading = ref(false)
const showHistory = ref(false)

const LEVEL_TAG = { readonly: 'warning', freeze: 'danger', handoff: 'danger' }
const KIND = {
  auth_failed: '验证失败', auth_passed: '验证通过', injection: '疑似注入', injection_high: '高置信注入',
  money_propose: '发起转账', tool_violation: '越权调用',
}
const SCOPE = { '*': '全部写操作', '@moves_money': '资金类操作' }

const active = computed(() => data.value?.trips.filter((t) => t.status === 'active') || [])
const history = computed(() => data.value?.trips.filter((t) => t.status !== 'active') || [])
const killOn = computed(() => !!data.value?.kill_switch?.enabled)

async function load() {
  loading.value = true
  try { data.value = await api.riskEvents() } catch (e) { showToast(e.message) } finally { loading.value = false }
}

async function release(t) {
  try {
    await showConfirmDialog({ title: '解除熔断', message: `${t.level_label}：${t.reason}\n确认已核实为本人操作？` })
  } catch { return }
  await api.releaseTrip(t.id, '运营后台核实后解除')
  showToast('已解除，已写入审计')
  load()
}

async function resetCircuit(c) {
  await api.resetCircuit(c.tool)
  showToast('断路器已重置')
  load()
}

async function toggleKill(on) {
  await api.killSwitch(on, on ? '运营后台手动开启' : '')
  load()
}

function tools(t) { return t.tools.map((x) => SCOPE[x] || x).join('、') }
function eventDetail(e) {
  return e.detail?.flags?.join('、') || e.tool || ''
}
function trigger(t) {
  const cond = `${t.window_s ? `${Math.round(t.window_s / 60)} 分钟内` : t.scope === 'session' ? '同一会话' : ''}第 ${t.count} 次`
  const how = { readonly: '会话只读', freeze: '暂停办理', handoff: '转人工' }[t.level]
  return `${KIND[t.event] || t.event}${t.per_tool ? '（同一业务' + (t.reset_on ? '，连续' : '') + '）' : ''} · ${cond} → ${how}${t.duration_s ? ` ${t.duration_s / 60} 分钟` : ''}`
}

onMounted(load)
defineExpose({ refresh: load })
</script>

<template>
  <div v-if="data" class="risk">
    <div class="kill">
      <div>
        <b>全局只读开关</b>
        <div class="sub">开启后所有写操作暂停，锁卡、挂失等止损操作不受影响</div>
      </div>
      <van-switch :model-value="killOn" size="20" active-color="#cf1322" @update:model-value="toggleKill" />
    </div>

    <div class="sec">
      <b>当前处置（{{ active.length }}）</b>
      <van-button size="mini" plain icon="replay" :loading="loading" @click="load">刷新</van-button>
    </div>
    <div v-if="!active.length" class="empty">没有生效中的熔断</div>
    <div v-for="t in active" :key="t.id" class="trip">
      <div class="trip-head">
        <van-tag :type="LEVEL_TAG[t.level]" size="medium">{{ t.level_label }}</van-tag>
        <span class="reason">{{ t.reason }}</span>
      </div>
      <div class="kv">范围：{{ t.scope === 'session' ? '本会话' : '用户级' }} · {{ tools(t) }}</div>
      <div class="kv">触发：{{ t.created_at.slice(11, 19) }} · {{ t.expires_at ? `${t.expires_at.slice(11, 16)} 自动恢复` : '需人工解除' }}</div>
      <div class="notice">用户看到的提示：{{ t.notice }}</div>
      <div class="trip-btns">
        <van-button v-if="t.trace_id" size="mini" plain @click="emit('replay', t.trace_id)">回放触发的那一轮</van-button>
        <van-button size="mini" type="danger" @click="release(t)">人工解除</van-button>
      </div>
    </div>

    <template v-if="data.circuits.length">
      <div class="sec"><b>工具断路器</b></div>
      <div v-for="c in data.circuits" :key="c.tool" class="circuit">
        <span class="mono">{{ c.tool }}</span>
        <van-tag :type="c.state === 'open' ? 'danger' : 'warning'">{{ { open: '断开', half_open: '半开探测' }[c.state] || c.state }}</van-tag>
        <span class="err">{{ c.last_error }}</span>
        <van-button size="mini" plain @click="resetCircuit(c)">重置</van-button>
      </div>
    </template>

    <div class="sec"><b>最近的安全事件</b></div>
    <div v-if="!data.events.length" class="empty">暂无</div>
    <div v-for="e in data.events.slice(0, 12)" :key="e.id" class="event">
      <span class="at">{{ e.at.slice(11, 19) }}</span>
      <span class="kind" :class="{ hot: e.kind !== 'auth_passed' && e.kind !== 'money_propose' }">{{ KIND[e.kind] || e.kind }}</span>
      <span class="det">{{ eventDetail(e) }}</span>
    </div>

    <div v-if="history.length" class="sec">
      <b>历史处置（{{ history.length }}）</b>
      <van-button size="mini" plain @click="showHistory = !showHistory">{{ showHistory ? '收起' : '展开' }}</van-button>
    </div>
    <template v-if="showHistory">
      <div v-for="t in history" :key="t.id" class="event">
        <span class="at">{{ t.created_at.slice(11, 19) }}</span>
        <span class="kind">{{ t.level_label }}</span>
        <span class="det">{{ t.reason }} · {{ t.status === 'released' ? `${t.released_by} 解除` : '已到期' }}</span>
      </div>
    </template>

    <div class="sec"><b>阈值配置</b><span class="sub">breaker.yaml · {{ data.config.version }}</span></div>
    <div v-for="t in data.config.triggers" :key="t.id" class="rule">{{ trigger(t) }}</div>
    <div class="rule">工具断路器 · {{ data.config.circuit.window_s }} 秒内错误率 &gt; {{ data.config.circuit.error_rate * 100 }}%
      → 断开 {{ data.config.circuit.cooldown_s }} 秒后半开探测</div>
    <div class="appeal">申诉渠道：{{ data.config.appeal }}</div>
  </div>
  <div v-else class="empty">加载中…</div>
</template>

<style scoped>
.kill { display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 10px; border-radius: 10px; background: #f7f8fa; }
.sub { font-size: 11px; color: var(--muted); margin-left: 6px; font-weight: 400; }
.kill .sub { margin: 2px 0 0; }
.sec { display: flex; justify-content: space-between; align-items: center; margin: 14px 0 6px; font-size: 14px; }
.empty { font-size: 13px; color: var(--muted); }
.trip { border: 1px solid var(--line); border-left: 3px solid var(--red); border-radius: 8px; padding: 8px 10px; margin-bottom: 8px; }
.trip-head { display: flex; align-items: center; gap: 8px; }
.reason { font-weight: 600; font-size: 14px; }
.kv { font-size: 12px; color: #4a5160; margin-top: 4px; }
.notice { font-size: 12px; color: var(--muted); margin-top: 4px; line-height: 1.5; }
.trip-btns { display: flex; gap: 6px; justify-content: flex-end; margin-top: 6px; }
.circuit { display: flex; align-items: center; gap: 6px; font-size: 12px; padding: 4px 0; }
.mono { font-family: ui-monospace, Menlo, monospace; }
.err { flex: 1; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.event { display: flex; gap: 8px; font-size: 12px; padding: 3px 0; border-bottom: 1px dashed var(--line); }
.at { color: var(--muted); flex-shrink: 0; }
.kind { flex-shrink: 0; font-weight: 600; }
.kind.hot { color: var(--red); }
.det { color: #4a5160; word-break: break-all; }
.rule { font-size: 12px; color: #4a5160; padding: 3px 0; }
.appeal { font-size: 12px; margin-top: 8px; padding: 8px; border-radius: 8px; background: var(--yellow-soft); color: #874d00; }
</style>
