<script setup>
// 知识检索设置：可选的向量通道（硅基流动 / 智谱 / 其他 OpenAI 兼容接口），密钥写进仓库根目录的 .env，保存后立即生效，
// 后端在后台算知识库条目向量。不配置或停用时只用关键词检索（BM25）。密钥不会从后端回传，这里只显示「已填写 · 尾号 ab12」。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { showToast } from 'vant'
import { api } from '../api'

const cfg = ref(null)
const provider = ref('siliconflow')
const apiKey = ref('')
const model = ref('')
const baseUrl = ref('')
const saving = ref(false)
const testing = ref(false)
const testResult = ref(null)
let timer = null

const current = computed(() => cfg.value?.providers.find((p) => p.id === provider.value))
const switching = computed(() => cfg.value && provider.value !== cfg.value.provider)
const dirty = computed(() => Boolean(cfg.value && ((switching.value && current.value?.has_key) || apiKey.value.trim()
  || model.value.trim() || baseUrl.value.trim())))
const tone = computed(() => ({ ready: 'on', off: 'idle', idle: 'wait', building: 'wait' })[cfg.value?.state] || 'bad')
const hint = computed(() => ({
  off: '知识问答按关键词匹配；配置向量后能听懂换了说法的问题，接口出错时自动退回关键词检索',
  idle: '下次提问时开始计算条目向量',
  building: '通常十几秒，算好前照常用关键词检索',
  ready: `${cfg.value?.docs} 条知识已向量化 · ${cfg.value?.model}${cfg.value?.dimensions ? ` · ${cfg.value.dimensions} 维` : ''}`,
})[cfg.value?.state] || '')

function apply(c) {
  cfg.value = c
  provider.value = c.provider || 'siliconflow'
  apiKey.value = ''
  model.value = ''
  baseUrl.value = ''
}

async function refresh() {
  try { cfg.value = await api.embeddingConfig() } catch (e) { showToast(e.message) }
}

function poll() {
  // 保存后条目向量在后台算：每秒刷新一次，算好、出错或 60 秒后停
  clearInterval(timer)
  let n = 0
  timer = setInterval(async () => {
    await refresh()
    if (++n >= 60 || !['building', 'idle'].includes(cfg.value?.state)) clearInterval(timer)
  }, 1000)
}

onMounted(async () => {
  try { apply(await api.embeddingConfig()) } catch (e) { showToast(e.message) }
  if (cfg.value?.state === 'building') poll()
})
onBeforeUnmount(() => clearInterval(timer))

async function save(disable = false) {
  saving.value = true
  testResult.value = null
  try {
    const body = disable ? { disable: true }
      : { provider: provider.value, api_key: apiKey.value.trim(), model: model.value.trim(), base_url: baseUrl.value.trim() }
    apply(await api.saveEmbeddingConfig(body))
    showToast(disable ? '已停用，只用关键词检索' : '已保存，正在计算知识库向量…')
    if (!disable) poll()
  } catch (e) {
    showToast(e.message)
  } finally {
    saving.value = false
  }
}

async function test() {
  testing.value = true
  testResult.value = null
  try { testResult.value = await api.testEmbedding() } catch (e) { testResult.value = { ok: false, error: e.message } }
  finally { testing.value = false }
}
</script>

<template>
  <div v-if="cfg" class="settings">
    <div class="status" :class="tone">
      <b>{{ cfg.state_text }}</b>
      <span v-if="hint">{{ hint }}</span>
      <span v-if="cfg.state === 'failed' && cfg.last_error" class="err">{{ cfg.last_error }}{{ cfg.retry_in_s ? `（${cfg.retry_in_s} 秒后重试）` : '' }}</span>
    </div>

    <div class="section">向量模型（三选一）</div>
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
                 :placeholder="provider === cfg.provider && cfg.model ? `当前 ${cfg.model}；填写则替换` : (current?.model || '如 text-embedding-3-small')" />
      <van-field v-if="provider === 'openai'" v-model="baseUrl" label="接口地址" autocomplete="off" clearable :label-width="70"
                 :placeholder="provider === cfg.provider && cfg.base_url ? `当前 ${cfg.base_url}` : 'https://api.example.com/v1'" />
    </van-cell-group>

    <div class="actions">
      <van-button type="danger" round block :loading="saving" :disabled="!dirty" @click="save()">保存并生效</van-button>
      <van-button round block plain :loading="testing" :disabled="!cfg.ready || dirty" @click="test">测试连接</van-button>
      <van-button v-if="cfg.provider" round block plain :loading="saving" @click="save(true)">停用，只用关键词检索</van-button>
    </div>
    <div v-if="testResult" class="result" :class="testResult.ok ? 'ok' : 'none'">
      {{ testResult.ok ? `连接正常：${testResult.model}，${testResult.dimensions} 维，用时 ${(testResult.latency_ms / 1000).toFixed(1)} 秒` : `连接失败：${testResult.error}` }}
    </div>

    <div class="hint">向量在服务端算，不占手机和电脑算力。提问会先脱敏（卡号、手机号打码）再发给向量接口；超过 2 秒没返回或出错时，这一问自动只用关键词检索。
      知识库条目的向量缓存在 data/kb_embeddings.json，条目改了才重算。</div>
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
.status.idle, .status.wait { background: var(--yellow-soft); }
.status.idle b, .status.wait b { color: var(--yellow-ink); }
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
