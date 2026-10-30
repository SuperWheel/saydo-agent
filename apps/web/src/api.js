// 运营后台口令只存在本标签页（关掉就要重新输），随 /admin 请求带上
const ADMIN_KEY = 'saydo-admin-password'
export const adminPassword = {
  get() { try { return sessionStorage.getItem(ADMIN_KEY) || '' } catch { return '' } },
  set(v) { try { sessionStorage.setItem(ADMIN_KEY, v) } catch { /* 隐私模式下存不了，就每次重新输 */ } },
}

async function request(method, url, body) {
  const headers = body === undefined ? {} : { 'Content-Type': 'application/json' }
  if (url.startsWith('/admin') && adminPassword.get()) headers['X-Admin-Password'] = adminPassword.get()
  const res = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) })
  const data = await res.json().catch(() => ({}))
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
}
