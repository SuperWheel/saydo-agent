async function request(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.detail || `请求失败（${res.status}）`)
  return data
}

const get = (url) => request('GET', url)
const post = (url, body = {}) => request('POST', url, body)

export const api = {
  health: () => get('/api/health'),
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
}
