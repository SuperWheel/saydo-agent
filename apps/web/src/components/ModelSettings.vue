<script setup>
// 模型设置：DeepSeek / 智谱 GLM 二选一，密钥写进仓库根目录的 .env，保存后立即生效。
// 密钥不会从后端回传，这里只显示「已填写 · 尾号 ab12」。
import { computed, onMounted, ref } from 'vue'
import { showToast } from 'vant'
import { adminPassword, api } from '../api'

const emit = defineEmits(['changed'])

const cfg = ref(null)
const provider = ref('deepseek')
const apiKey = ref('')
const enabled = ref(true)
const newPassword = ref('')
const saving = ref(false)
const testing = ref(false)
const testResult = ref(null)

const current = computed(() => cfg.value?.providers.find((p) => p.id === provider.value))
const dirty = computed(() => cfg.value && (provider.value !== cfg.value.provider || apiKey.value.trim()
  || enabled.value !== cfg.value.enabled || newPassword.value))

function apply(c) {
  cfg.value = c
  provider.value = c.provider
  enabled.value = c.enabled
  apiKey.value = ''
  newPassword.value = ''
}

onMounted(async () => {
  try { apply(await api.llmConfig()) } catch (e) { showToast(e.message) }
})

async function save() {
  saving.value = true
  testResult.value = null
  try {
    const body = { provider: provider.value, api_key: apiKey.value.trim(), enabled: enabled.value }
    if (newPassword.value) body.admin_password = newPassword.value
    const c = await api.saveLlmConfig(body)
    if (newPassword.value) adminPassword.set(newPassword.value)
    apply(c)
    emit('changed', c)
    showToast(c.ready ? `已保存，现在使用 ${c.model}` : '已保存，现在走规则模式')
  } catch (e) {
    showToast(e.message)
  } finally {
    saving.value = false
  }
}

async function test() {
  testing.value = true
  testResult.value = null
  try { testResult.value = await api.testLlm() } catch (e) { testResult.value = { ok: false, error: e.message } }
  finally { testing.value = false }
}
</script>

<template>
  <div v-if="cfg" class="settings">
    <div class="status" :class="cfg.ready ? 'on' : 'off'">
      <b>{{ cfg.ready ? `正在使用 ${cfg.model}` : '规则模式' }}</b>
      <span>{{ cfg.ready ? '大模型理解 + 规则把关' : '没有可用的模型密钥，或已关闭大模型；只用规则识别' }}</span>
    </div>

    <div class="section">选择模型（二选一）</div>
    <van-radio-group v-model="provider">
      <van-cell-group inset>
        <van-cell v-for="p in cfg.providers" :key="p.id" clickable @click="provider = p.id">
          <template #title>
            <span>{{ p.label }}</span><small class="model">{{ p.model }}</small>
          </template>
          <template #label>
            <span :class="p.has_key ? 'ok' : 'none'">{{ p.has_key ? `已填写 · 尾号 ${p.key_tail || '****'}` : '未填写密钥' }}</span>
          </template>
          <template #right-icon><van-radio :name="p.id" checked-color="#c8102e" /></template>
        </van-cell>
      </van-cell-group>
    </van-radio-group>

    <van-cell-group inset class="group">
      <van-field v-model="apiKey" type="password" autocomplete="off" label="密钥"
                 :placeholder="current?.has_key ? '留空则沿用已填写的密钥' : `粘贴 ${current?.label} 的 API Key`" clearable />
      <van-cell title="启用大模型" label="关闭后只走规则模式（断网演示用）">
        <template #right-icon><van-switch v-model="enabled" size="20" active-color="#c8102e" /></template>
      </van-cell>
    </van-cell-group>

    <div class="actions">
      <van-button type="danger" round block :loading="saving" :disabled="!dirty" @click="save">保存并生效</van-button>
      <van-button round block plain :loading="testing" :disabled="!cfg.ready || dirty" @click="test">测试连接</van-button>
    </div>
    <div v-if="testResult" class="result" :class="testResult.ok ? 'ok' : 'none'">
      {{ testResult.ok ? `连接正常：${testResult.model}，用时 ${(testResult.latency_ms / 1000).toFixed(1)} 秒` : `连接失败：${testResult.error}` }}
    </div>

    <div class="section">运营后台口令</div>
    <van-cell-group inset>
      <van-field v-model="newPassword" type="password" autocomplete="new-password" label="新口令"
                 :placeholder="cfg.admin_protected ? '已设置；填写则修改' : '未设置（仅演示模式开放）'" clearable />
    </van-cell-group>

    <div v-for="w in cfg.warnings" :key="w" class="warn">⚠️ {{ w }}</div>
    <div class="foot">保存位置：{{ cfg.env_file }}{{ cfg.env_file_exists ? '' : '（将新建）' }}。密钥只保存在本机，不会显示、不进审计日志。</div>
  </div>
  <van-loading v-else class="loading" />
</template>

<style scoped>
.settings { padding-bottom: 8px; }
.status { margin: 0 16px 6px; padding: 10px 12px; border-radius: 10px; display: flex; flex-direction: column; gap: 2px; }
.status span { font-size: 12px; color: var(--muted); }
.status.on { background: var(--green-soft); }
.status.on b { color: var(--green); }
.status.off { background: var(--yellow-soft); }
.status.off b { color: var(--yellow); }
.section { font-size: 13px; color: var(--muted); margin: 14px 16px 6px; }
.group { margin-top: 10px; }
.model { color: var(--muted); margin-left: 6px; font-size: 12px; }
.ok { color: var(--green); }
.none { color: var(--muted); }
.actions { display: flex; flex-direction: column; gap: 8px; margin: 14px 16px 0; }
.result { margin: 8px 16px 0; font-size: 13px; word-break: break-all; }
.result.none { color: var(--red); }
.warn { margin: 10px 16px 0; font-size: 12px; color: var(--yellow); }
.foot { margin: 12px 16px 0; font-size: 11px; color: var(--muted); line-height: 1.5; word-break: break-all; }
.loading { display: flex; justify-content: center; padding: 24px; }
</style>
