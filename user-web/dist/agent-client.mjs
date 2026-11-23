const storageKey = 'qibu-agent-session-v1'

function sessionId() {
  const created = crypto.randomUUID()
  try {
    const existing = localStorage.getItem(storageKey)
    if (existing && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(existing)) return existing
    localStorage.setItem(storageKey, created)
  } catch {
    // The current page can still use the server when local storage is unavailable.
  }
  return created
}

export function createAgentClient() {
  let id = sessionId()
  function select(session) {
    id = session
    try { localStorage.setItem(storageKey, session) } catch { /* Use the live session ID. */ }
  }
  async function request(path, data) {
    let response
    try {
      const signal = AbortSignal.timeout(75_000)
      response = await fetch('/api/qibu/' + path, data ? {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: id, ...data }),
        signal,
      } : { signal })
    } catch (error) {
      if (error.name === 'TimeoutError') throw new Error('这次请求等待超时。你可以再试一次。')
      throw new Error('后端暂时连不上。你可以再试一次。')
    }
    const body = await response.json().catch(() => null)
    if (!response.ok || !body) {
      const error = new Error(typeof body?.detail === 'string' ? body.detail : '请求未完成，请再试一次。')
      error.status = response.status
      throw error
    }
    return body
  }
  return {
    current: () => id,
    select,
    conversations: () => request('conversations'),
    newConversation: () => request('conversations', {}),
    read: () => request('session?session_id=' + encodeURIComponent(id)),
    chat: (message, turnId) => request('chat', { message, turn_id: turnId }),
    change: (action, operation, day) => request('actions', { action_id: action, operation, day }),
    memory: (operation, draft) => request('memory', { operation, draft_id: draft }),
  }
}
