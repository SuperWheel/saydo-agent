<script setup>
// 知识检索 · 重排模型卡片（可选）：硅基流动（Qwen3-Reranker-4B）/ 千问（qwen3-rerank），密钥写进仓库根目录的 .env，保存后立即生效。
// 重排把检索出的前 10 条候选逐条打相关性分，按分数重排，最高分过门槛才算查到。不配置或停用时不重排。
// 密钥不会从后端回传，这里只显示「已填写 · 尾号 ab12」。状态变了就 emit('changed')，外层卡片头部跟着更新。
import { computed, onMounted, ref, watch } from 'vue'
import { showToast } from 'vant'
import { api } from '../api'

const emit = defineEmits(['changed'])
const cfg = ref(null)
watch(cfg, (c) => c && emit('changed', c))
const provider = ref('siliconflow')
const apiKey = ref('')
const model = ref('')
const baseUrl = ref('')
const saving = ref(false)
const testing = ref(false)
const testResult = ref(null)

const current = computed(() => cfg.value?.providers.find((p) => p.id === provider.value))
const switching = computed(() => cfg.value && provider.value !== cfg.value.provider)
const dirty = computed(() => Boolean(cfg.value && ((switching.value && current.value?.has_key) || apiKey.value.trim()
  || model.value.trim() || baseUrl.value.trim())))
const tone = computed(() => ({ ready: 'on', off: 'idle' })[cfg.value?.state] || 'bad')
const hint = computed(() => {
  const c = cfg.value
  if (!c) return ''
  if (c.state === 'off') return '直接按检索的排序和门槛作答；配置后把前 10 条候选交给重排模型逐条打分，答得更准、拒答更稳'
  if (c.state === 'ready') {
    return `${c.model} · 查到门槛 ${c.min_score} · 时限 ${c.timeout_s} 秒 · 候选来自${c.with_embedding ? '关键词 + 向量' : '关键词'}检索`
  }
  return ''
})

function apply(c) {
  cfg.value = c
  provider.value = c.provider || 'siliconflow'
  apiKey.value = ''
  model.value = ''
  baseUrl.value = ''
}

onMounted(async () => {
  try { apply(await api.rerankConfig()) } catch (e) { showToast(e.message) }
})

async function save(disable = false) {
  saving.value = true
  testResult.value = null
  try {
    const body = disable ? { disable: true }
      : { provider: provider.value, api_key: apiKey.value.trim(), model: model.value.trim(), base_url: baseUrl.value.trim() }
    apply(await api.saveRerankConfig(body))
    showToast(disable ? '已停用，不重排' : '已保存，重排已生效')
  } catch (e) {
    showToast(e.message)
  } finally {
    saving.value = false
  }
}

async function test() {
  testing.value = true
  testResult.value = null
  try { testResult.value = await api.testRerank() } catch (e) { testResult.value = { ok: false, error: e.message } }
  finally { testing.value = false }
}

const testText = computed(() => {
  const r = testResult.value
  if (!r) return ''
  if (!r.ok) return `连接失败：${r.error}`
  const order = r.sensible ? '排序正确' : '排序不对，检查模型名'
  return `连接正常：${r.model}，用时 ${(r.latency_ms / 1000).toFixed(1)} 秒，两条示例打分 ${r.scores.join(' / ')}（${order}）`
})
</script>

<template>
  <div v-if="cfg" class="settings">
    <div class="status" :class="tone">
      <b>{{ cfg.state_text }}</b>
      <span v-if="hint">{{ hint }}</span>
      <span v-if="cfg.state === 'failed' && cfg.last_error" class="err">{{ cfg.last_error }}{{ cfg.retry_in_s ? `（${cfg.retry_in_s} 秒后重试）` : '' }}</span>
    </div>

    <div class="section">重排模型（选一个）</div>
    <van-radio-group v-model="provider">
      <van-cell-group inset>
        <van-cell v-for="p in cfg.providers" :key="p.id" clickable @click="provider = p.id">
          <template #title>
            <span>{{ p.label }}</span><small v-if="p.model" class="model">{{ p.model }}</small>
          </template>
          <template #label>
            <div class="note">{{ p.note }}</div>
            <span :class="p.has_key ? 'ok' : 'none'">{{ p.has_key ? `已填写 · 尾号 ${p.key_tail || '****'}` : '未填写密钥' }}</span>
          </template>
          <template #right-icon><van-radio :name="p.id" checked-color="var(--brand)" /></template>
        </van-cell>
      </van-cell-group>
    </van-radio-group>

    <van-cell-group inset class="group">
      <van-field v-model="apiKey" type="password" autocomplete="off" label="密钥" clearable
                 :placeholder="current?.has_key ? '留空则沿用已填写的密钥' : `粘贴${current?.label}的 API Key`" />
      <van-field v-model="model" label="模型" autocomplete="off" clearable
                 :placeholder="provider === cfg.provider && cfg.model ? `当前 ${cfg.model}；填写则替换` : current?.model" />
      <van-field v-model="baseUrl" label="接口地址" autocomplete="off" clearable :label-width="70"
                 :placeholder="provider === cfg.provider && cfg.base_url ? `当前 ${cfg.base_url}` : `留空用 ${current?.base_url}`" />
    </van-cell-group>

    <div class="actions">
      <van-button type="danger" round block :loading="saving" :disabled="!dirty" @click="save()">保存并生效</van-button>
      <van-button round block plain :loading="testing" :disabled="!cfg.ready || dirty" @click="test">测试连接</van-button>
      <van-button v-if="cfg.provider" round block plain :loading="saving" @click="save(true)">停用，不重排</van-button>
    </div>
    <div v-if="testResult" class="result" :class="testResult.ok ? 'ok' : 'none'">{{ testText }}</div>

    <div class="hint">重排在服务端调接口，提问先脱敏（卡号、手机号打码）再发；每问多一次网络请求（Qwen3-Reranker-4B 实测单题检索中位约 0.9 秒），
      超过时限没返回或出错时，这一问不重排、照常作答。千问 qwen3-rerank 按百炼文档接入，还没用真实密钥测过，门槛也没校准。</div>
    <div v-for="w in cfg.warnings" :key="w" class="warn"><IconText :text="`⚠️ ${w}`" /></div>
    <div class="foot">保存位置：{{ cfg.env_file }}。密钥只保存在本机，不会显示、不进审计日志。</div>
  </div>
  <van-loading v-else class="loading" />
</template>

<style scoped>
.settings { padding-bottom: 8px; }
.status { margin: 0 16px 6px; padding: 10px 12px; border-radius: var(--radius-md); display: flex; flex-direction: column; gap: 2px; }
.status span { font-size: var(--fs-xs); color: var(--muted); word-break: break-all; }
.status.on { background: var(--green-soft); }
.status.on b { color: var(--green-ink); }
.status.idle { background: var(--yellow-soft); }
.status.idle b { color: var(--yellow-ink); }
.status.bad { background: var(--red-soft); }
.status.bad b, .status .err { color: var(--red); }
.section { font-size: var(--fs-sm); color: var(--muted); margin: 14px 16px 6px; }
.group { margin-top: 10px; }
.model { color: var(--muted); margin-left: 6px; font-size: var(--fs-xs); }
.note { color: var(--muted); font-size: var(--fs-2xs); line-height: 1.5; margin-bottom: 2px; }
.ok { color: var(--green-ink); }
.none { color: var(--muted); }
.actions { display: flex; flex-direction: column; gap: 8px; margin: 14px 16px 0; }
.result { margin: 8px 16px 0; font-size: var(--fs-sm); word-break: break-all; }
.result.none { color: var(--red); }
.hint { margin: 10px 16px 0; font-size: var(--fs-xs); color: var(--muted); line-height: 1.5; }
.warn { margin: 10px 16px 0; font-size: var(--fs-xs); color: var(--yellow-ink); }
.foot { margin: 12px 16px 0; font-size: var(--fs-2xs); color: var(--muted); line-height: 1.5; word-break: break-all; }
.loading { display: flex; justify-content: center; padding: 24px; }
</style>
