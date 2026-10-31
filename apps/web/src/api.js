// 运营后台口令只存在本标签页（关掉就要重新输），随 /admin 请求带上
const ADMIN_KEY = 'saydo-admin-password'
export const adminPassword = {
  get() { try { return sessionStorage.getItem(ADMIN_KEY) || '' } catch { return '' } },
  set(v) { try { sessionStorage.setItem(ADMIN_KEY, v) } catch { /* 隐私模式下存不了，就每次重新输 */ } },
}

// 进入前人脸核身的凭证（后端签发，30 分钟有效）也只存在本标签页：刷新页面不用重验，关掉标签页要重验。
// 到期时间按本机收到凭证的时刻 + ttl 算，不依赖手机和服务器的时钟一致
const ENTRY_KEY = 'saydo-entry'
let onEntryLost = () => {}
export const entryPass = {
  get() {
    try {
      const p = JSON.parse(sessionStorage.getItem(ENTRY_KEY) || 'null')
      return p && p.until > Date.now() ? p : null
    } catch { return null }
  },
  set(token, ttlS) {
    const p = { token, until: Date.now() + ttlS * 1000 }
    try { sessionStorage.setItem(ENTRY_KEY, JSON.stringify(p)) } catch { /* 存不了就只在内存里，刷新后重验 */ }
    memo = p
    return p
  },
  clear() { memo = null; try { sessionStorage.removeItem(ENTRY_KEY) } catch { /* 同上 */ } },
  token() { return (entryPass.get() || (memo && memo.until > Date.now() ? memo : null))?.token || '' },
  onLost(fn) { onEntryLost = fn }, // 后端说凭证无效（过期、重置演示数据）时回到人脸页
}
let memo = null

async function request(method, url, body) {
  const headers = body === undefined ? {} : { 'Content-Type': 'application/json' }
  if (url.startsWith('/admin') && adminPassword.get()) headers['X-Admin-Password'] = adminPassword.get()
  if (url.startsWith('/api') && entryPass.token()) headers['X-Entry-Token'] = entryPass.token()
  const res = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) })
  const data = await res.json().catch(() => ({}))
  if (res.status === 401 && res.headers.get('X-Entry-Required')) {
    entryPass.clear()
    onEntryLost(data.detail)
  }
  if (!res.ok) {
    const err = new Error(data.detail || `请求失败（${res.status}）`)
    err.status = res.status
    throw err
  }
  return data
}

const get = (url) => request('GET', url)
const post = (url, body = {}) => request('POST', url, body)

export const api = {
  health: () => get('/api/health'),
  entryFace: () => post('/api/auth/entry-face', { value: 'face-ok:entry' }), // 模拟活体检测通过
  billTxns: (params) => get(`/api/bill/txns?${new URLSearchParams(params)}`),
  wealthProduct: (code) => get(`/api/wealth/products/${encodeURIComponent(code)}`),
  cardDetail: (id) => get(`/api/cards/${encodeURIComponent(id)}`),
  cardReveal: (id) => post(`/api/cards/${encodeURIComponent(id)}/reveal`),
  cardRevealVerify: (challengeId, value) => post(`/api/cards/reveals/${encodeURIComponent(challengeId)}/verify`, { factor: 'face', value }),
  handoff: (code) => get(`/api/handoff/${encodeURIComponent(code)}`),
  chat: (body) => post('/api/chat', body),
  confirm: (actionId, digest) => post(`/api/actions/${actionId}/confirm`, { digest }),
  cancel: (actionId) => post(`/api/actions/${actionId}/cancel`),
  verify: (challengeId, factor, value) => post(`/api/auth/challenges/${challengeId}/verify`, { factor, value }),
  audit: (traceId) => get(`/admin/audit?trace_id=${encodeURIComponent(traceId)}`),
  verifyAudit: () => post('/admin/audit/verify'),
  traces: (limit = 30) => get(`/admin/traces?limit=${limit}`),
  riskEvents: () => get('/admin/risk-events'),
  releaseTrip: (tripId, note) => post(`/admin/breaker/trips/${tripId}/release`, { operator: '运营后台', note }),
  breakerSupport: (tripId) => get(`/api/breaker/trips/${encodeURIComponent(tripId)}/support`),
  breakerReview: (tripId, body) => post(`/api/breaker/trips/${encodeURIComponent(tripId)}/support`, body),
  resetCircuit: (tool) => post(`/admin/breaker/circuits/${encodeURIComponent(tool)}/reset`, { operator: '运营后台' }),
  killSwitch: (enabled, reason) => post('/admin/kill-switch', { enabled, reason }),
  reset: () => post('/dev/seed'),
  aaPay: (shareId) => post(`/dev/aa/shares/${shareId}/pay`),
  planSync: (sessionId) => post('/api/plans/sync', { session_id: sessionId }),
  fastForward: (to) => post('/dev/fast-forward', { to }),
  llmConfig: () => get('/admin/llm-config'),
  saveLlmConfig: (body) => post('/admin/llm-config', body),
  testLlm: () => post('/admin/llm-config/test'),
  wecomConfig: () => get('/admin/wecom-config'),
  saveWecomConfig: (body) => post('/admin/wecom-config', body),
  embeddingConfig: () => get('/admin/embedding-config'),
  saveEmbeddingConfig: (body) => post('/admin/embedding-config', body),
  testEmbedding: () => post('/admin/embedding-config/test'),
  rerankConfig: () => get('/admin/rerank-config'),
  saveRerankConfig: (body) => post('/admin/rerank-config', body),
  testRerank: () => post('/admin/rerank-config/test'),
}
