import type { Progress, Turn } from './types'

export async function streamTurn(question: string, session: string, onProgress: (event: Progress) => void): Promise<Turn> {
  const response = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ session_id: session, message: question }),
  })
  if (!response.ok || !response.body) throw new Error('请求未完成，请再试一次。')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let pending = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      pending += decoder.decode(value, { stream: true })
      const records = pending.split('\n\n')
      pending = records.pop() ?? ''
      for (const record of records) {
        const kind = record.split('\n').find(line => line.startsWith('event: '))?.slice(7)
        const data = record.split('\n').find(line => line.startsWith('data: '))?.slice(6)
        if (!data) continue
        if (kind === 'progress') onProgress(JSON.parse(data) as Progress)
        if (kind === 'error') throw new Error(JSON.parse(data).detail)
        if (kind === 'done') return JSON.parse(data) as Turn
      }
    }
  } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
  throw new Error('连接在收到回复前结束，请再试一次。')
}
