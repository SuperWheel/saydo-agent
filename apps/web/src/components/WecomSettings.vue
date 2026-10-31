<script setup>
// 企业微信设置：智能机器人的 Bot ID、Secret 写进仓库根目录的 .env，保存后后端当场重连，不用重启。
// Secret 不会从后端回传，这里只显示「已填写」。教程截图在 public/wecom-guide/。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { showImagePreview, showToast } from 'vant'
import { api } from '../api'

const GUIDE = [
  { img: '/wecom-guide/1-entry.webp', title: '打开智能机器人',
    text: '用管理员登录企业微信管理后台（work.weixin.qq.com），左侧点「智能专区」→「智能机器人」。' },
  { img: '/wecom-guide/2-create.webp', title: '新建机器人',
    text: '左侧选「创建」，点蓝色的「新建机器人」。不要用下面的「选择场景，快速开始」，那些没有 API 模式。' },
  { img: '/wecom-guide/3-api-mode.webp', title: '选 API 模式和长连接，取凭证',
    text: '填头像和名称（如「言行助手」）；模式选「API 模式」；连接方式选「使用长连接」。复制 Bot ID，Secret 点「点击获取」后复制，最后点「保存」。' },
]
const TRY = ['我还有多少钱', '上个月的账单报告', '锁卡和挂失有什么区别', '给我妈转500']

const cfg = ref(null)
const botId = ref('')
const secret = ref('')
const h5 = ref('')
const saving = ref(false)
const guideOpen = ref(['guide'])
let timer = null

const tone = computed(() => ({ connected: 'on', off: 'idle', connecting: 'wait', reconnecting: 'wait' })[cfg.value?.state] || 'bad')
const dirty = computed(() => botId.value.trim() || secret.value.trim() || (h5.value.trim() && h5.value.trim() !== cfg.value?.app_h5_url))

function apply(c) {
  cfg.value = c
  if (!h5.value) h5.value = c.app_h5_url
  if (c.configured) guideOpen.value = []
}

async function refresh() {
  try { apply(await api.wecomConfig()) } catch (e) { showToast(e.message) }
}

function poll() {
  // 保存后长连接要一两秒才有结果：每秒刷新一次，连上、被拒或 20 秒后停
  clearInterval(timer)
  let n = 0
  timer = setInterval(async () => {
    await refresh()
    if (++n >= 20 || !['connecting', 'reconnecting'].includes(cfg.value?.state)) clearInterval(timer)
  }, 1000)
}

onMounted(refresh)
onBeforeUnmount(() => clearInterval(timer))

async function save(disable = false) {
  saving.value = true
  try {
    const body = disable ? { disable: true } : { bot_id: botId.value.trim(), secret: secret.value.trim(), app_h5_url: h5.value.trim() }
    apply(await api.saveWecomConfig(body))
    botId.value = ''
    secret.value = ''
    showToast(disable ? '已停用企业微信渠道' : '已保存，正在连接企业微信…')
    if (!disable) poll()
  } catch (e) {
    showToast(e.message)
  } finally {
    saving.value = false
  }
}

function preview(i) {
  showImagePreview({ images: GUIDE.map((g) => g.img), startPosition: i, closeable: true })
}
</script>

<template>
  <div v-if="cfg" class="settings">
    <div class="status" :class="tone">
      <b>{{ cfg.state_text }}</b>
      <span v-if="cfg.state === 'connected'">在企业微信里找到机器人私聊即可，只做只读查询，办理类操作会引导回 App</span>
      <span v-else-if="cfg.state === 'off'">填好 Bot ID 和 Secret 保存后自动连接；不配置也不影响 App 使用</span>
      <span v-else-if="cfg.last_error" class="err">{{ cfg.last_error }}</span>
    </div>

    <van-collapse v-model="guideOpen" :border="false">
      <van-collapse-item title="配置教程（3 步，约 2 分钟）" name="guide">
        <div v-for="(g, i) in GUIDE" :key="g.img" class="step">
          <div class="step-title"><span class="no">{{ i + 1 }}</span>{{ g.title }}</div>
          <div class="step-text">{{ g.text }}</div>
          <img :src="g.img" :alt="g.title" class="shot" loading="lazy" @click="preview(i)" />
        </div>
        <div class="step">
          <div class="step-title"><span class="no">4</span>粘贴到下面，保存并连接</div>
          <div class="step-text">状态变成「已连接」后，打开企业微信，在通讯录里搜机器人的名字，发起单聊，试试：</div>
          <div class="chips"><span v-for="t in TRY" :key="t" class="chip">{{ t }}</span></div>
          <div class="step-text muted">前三句直接回答；「给我妈转500」不会在企业微信里办理，只回 App 链接（渠道降权）。群聊里不查账户。</div>
        </div>
      </van-collapse-item>
    </van-collapse>

    <div class="section">机器人凭证</div>
    <van-cell-group inset>
      <van-field v-model="botId" label="Bot ID" autocomplete="off" clearable
                 :placeholder="cfg.bot_id ? `已填写 ${cfg.bot_id}；填写则替换` : '粘贴机器人的 Bot ID'" />
      <van-field v-model="secret" label="Secret" type="password" autocomplete="off" clearable
                 :placeholder="cfg.has_secret ? '已填写；留空则沿用' : '点「点击获取」后粘贴'" />
      <van-field v-model="h5" label="App 地址" autocomplete="off" clearable placeholder="http://127.0.0.1:5173"
                 :label-width="70" />
    </van-cell-group>
    <div class="link">企业微信里发出的链接：<b>{{ cfg.link_url }}</b></div>
    <div class="hint">「App 地址」保持默认的 127.0.0.1 时，链接会自动换成电脑当前的局域网地址，换网络也跟着变。
      手机要打得开：手机和电脑连同一网络（最稳的是手机开热点、电脑连上），照常 <code>make dev</code> 启动。部署到服务器后再把这里改成正式地址。</div>

    <div class="actions">
      <van-button type="danger" round block :loading="saving" :disabled="!dirty" @click="save()">保存并连接</van-button>
      <van-button v-if="cfg.configured" round block plain :loading="saving" @click="save(true)">停用企业微信渠道</van-button>
    </div>

    <div v-for="w in cfg.warnings" :key="w" class="warn">⚠️ {{ w }}</div>
    <div class="foot">保存位置：{{ cfg.env_file }}。Secret 只保存在本机，不会显示、不进审计日志。同一个机器人只能有一个连接，别在两台电脑上同时用同一套凭证。</div>
  </div>
  <van-loading v-else class="loading" />
</template>

<style scoped>
.settings { padding-bottom: 8px; }
.status { margin: 0 16px 6px; padding: 10px 12px; border-radius: 10px; display: flex; flex-direction: column; gap: 2px; }
.status span { font-size: 12px; color: var(--muted); word-break: break-all; }
.status.on { background: var(--green-soft); }
.status.on b { color: var(--green); }
.status.idle, .status.wait { background: var(--yellow-soft); }
.status.idle b, .status.wait b { color: var(--yellow); }
.status.bad { background: #fdecee; }
.status.bad b, .status .err { color: var(--red); }
.step { margin-bottom: 14px; }
.step-title { font-weight: 600; font-size: 14px; color: var(--text, #222); display: flex; align-items: center; gap: 6px; }
.no { display: inline-flex; width: 18px; height: 18px; border-radius: 50%; background: #c8102e; color: #fff;
  font-size: 11px; align-items: center; justify-content: center; }
.step-text { font-size: 12.5px; line-height: 1.6; margin: 4px 0 6px; color: #444; }
.muted { color: var(--muted); }
.shot { width: 100%; border-radius: 8px; border: 1px solid #eee; cursor: zoom-in; display: block; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
.chip { font-size: 12px; padding: 3px 9px; border-radius: 12px; background: #f4f5f7; color: #333; }
.section { font-size: 13px; color: var(--muted); margin: 14px 16px 6px; }
.link { margin: 8px 16px 0; font-size: 12px; color: var(--muted); word-break: break-all; }
.link b { color: var(--text, #222); font-weight: 600; }
.hint code { font-size: 11px; background: #f4f5f7; padding: 0 3px; border-radius: 3px; }
.hint { margin: 6px 16px 0; font-size: 11.5px; color: var(--muted); line-height: 1.5; }
.actions { display: flex; flex-direction: column; gap: 8px; margin: 14px 16px 0; }
.warn { margin: 10px 16px 0; font-size: 12px; color: var(--yellow); }
.foot { margin: 12px 16px 0; font-size: 11px; color: var(--muted); line-height: 1.5; word-break: break-all; }
.loading { display: flex; justify-content: center; padding: 24px; }
</style>
