import { useEffect, useRef, useState, type FormEvent } from 'react'
import { createRoot } from 'react-dom/client'
import {
  Activity, ArrowUpRight, BookOpen, Database, FlaskConical, HeartPulse, Layers3, LoaderCircle,
  MessageSquare, Play, RotateCcw, Send, Waypoints,
} from 'lucide-react'
import { EvolutionPanel } from './EvolutionPanel'
import { roleNames, TracePanel } from './TracePanel'
import { streamTurn } from './stream'
import type { Demo, Evolution, Progress, Turn } from './types'
import './styles.css'

const examples = [
  { id: 'joint', title: '步数与当晚深睡', description: 'DS → DE → HC', icon: Activity },
  { id: 'rag', title: '什么是深睡？', description: '本地证据检索', icon: BookOpen },
  { id: 'steps', title: '过去 7 天走了多少步？', description: '单 Agent 查询', icon: Waypoints },
] as const
type Page = 'chat' | 'evolution'
type Example = typeof examples[number]['id']

function pageFromUrl(): Page {
  return window.location.hash === '#evolution' ? 'evolution' : 'chat'
}

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(typeof body.detail === 'string' ? body.detail : '请求未完成，请再试一次。')
  }
  return response.json() as Promise<T>
}

function Reply({ turn }: { turn: Turn }) {
  const analysis = turn.details.sandbox_analyses[0]
  const evidence = turn.details.evidence_runs[0]
  const coach = turn.details.coaching_turns[0]
  if (analysis && evidence) {
    const result = analysis.result
    const difference = result?.difference_hours ?? 0
    const metric = analysis.plan?.sleep_metric === 'deep_sleep_hours' ? '深睡' : '睡眠'
    return <div className="reply-copy">
      {result ? <>
        <h3>{difference === 0 ? '两组的' + metric + '均值相同'
          : '高步数组的' + metric + '均值' + (difference > 0 ? '多' : '少') + '约 ' + Math.abs(difference * 60).toFixed(1) + ' 分钟'}</h3>
        <p>按入睡日期配对得到 {result.paired_days} 个有效日。高步数组平均 {result.high_mean_hours.toFixed(2)} 小时，其余组平均 {result.other_mean_hours.toFixed(2)} 小时。</p>
      </> : <p>{analysis.reply}</p>}
      {evidence.limitations[0] && <div className="evidence-note"><BookOpen size={16} /><p>{evidence.limitations[0]}</p></div>}
      {coach && <p className="follow-up">{coach.response}</p>}
      <details className="full-reply"><summary>查看完整回复</summary><p>{turn.reply.replace(/\[(.*?)\]\(<.*?>\)/g, '$1').replaceAll('**', '')}</p></details>
    </div>
  }
  return <div className="reply-copy"><p className="formatted-copy">{turn.reply.replace(/\[(.*?)\]\(<.*?>\)/g, '$1').replaceAll('**', '')}</p></div>
}

function App() {
  const [page, setPage] = useState(pageFromUrl)
  const [demo, setDemo] = useState<Demo | null>(null)
  const [evolution, setEvolution] = useState<Evolution | null>(null)
  const [history, setHistory] = useState<Turn[]>([])
  const [example, setExample] = useState<Example | null>('joint')
  const [session, setSession] = useState(() => crypto.randomUUID())
  const [message, setMessage] = useState('')
  const [pending, setPending] = useState('')
  const [progress, setProgress] = useState<Progress | null>(null)
  const [loadError, setLoadError] = useState('')
  const [chatError, setChatError] = useState('')
  const [evolutionError, setEvolutionError] = useState('')
  const [loadAttempt, setLoadAttempt] = useState(0)
  const [evolutionAttempt, setEvolutionAttempt] = useState(0)
  const messagesRef = useRef<HTMLDivElement>(null)
  const turn = history.at(-1) ?? demo?.replay ?? null

  useEffect(() => {
    const sync = () => setPage(pageFromUrl())
    window.addEventListener('hashchange', sync)
    return () => window.removeEventListener('hashchange', sync)
  }, [])

  useEffect(() => {
    let alive = true
    api<Demo>('/api/demo').then(value => { if (alive) setDemo(value) }).catch(error => {
      if (alive) setLoadError(error.message)
    })
    return () => { alive = false }
  }, [loadAttempt])

  useEffect(() => {
    if (page !== 'evolution' || evolution) return
    let alive = true
    api<Evolution>('/api/evolution').then(value => { if (alive) setEvolution(value) }).catch(error => {
      if (alive) setEvolutionError(error.message)
    })
    return () => { alive = false }
  }, [page, evolution, evolutionAttempt])

  useEffect(() => {
    if (messagesRef.current) messagesRef.current.scrollTop = messagesRef.current.scrollHeight
  }, [history, pending])

  function navigate(next: Page) {
    window.location.hash = next === 'evolution' ? 'evolution' : 'chat'
    setPage(next)
  }

  async function send(question: string) {
    if (!question.trim() || pending || !demo) return
    setChatError('')
    setPending(question)
    setProgress(null)
    setMessage('')
    try {
      const result = await streamTurn(question, session, setProgress)
      setHistory(previous => [...previous, result])
    } catch (error) {
      setChatError(error instanceof Error ? error.message : '请求未完成，请再试一次。')
      setMessage(question)
    } finally {
      setPending('')
    }
  }

  function chooseExample(id: Example) {
    navigate('chat')
    setExample(id)
    if (id === 'joint') {
      setChatError('')
      setHistory([])
      setMessage('')
      setSession(crypto.randomUUID())
    } else {
      void send(id === 'rag' ? '什么是深睡？' : '过去7天我一共走了多少步？')
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    setExample(null)
    void send(message)
  }

  const messages = history.length ? history : demo ? [demo.replay] : []
  const roles = [...new Set(turn?.details.trace.map(step => step.agent))]
  const analysis = turn?.details.sandbox_analyses[0]
  const hasLiveJoint = demo?.capabilities.joint_live

  return <div className="shell">
    <aside className="sidebar">
      <a className="brand" href="#chat" onClick={() => navigate('chat')}>
        <span className="brand-symbol"><HeartPulse size={23} strokeWidth={2.1} /></span>
        <span><strong>Personal Health</strong><small>Agent Lab</small></span>
      </a>
      <nav className="primary-nav" aria-label="页面">
        <button type="button" onClick={() => navigate('chat')} aria-current={page === 'chat' ? 'page' : undefined}><MessageSquare size={17} />对话工作台</button>
        <button type="button" onClick={() => navigate('evolution')} aria-current={page === 'evolution' ? 'page' : undefined}><FlaskConical size={17} />Skill 自进化</button>
      </nav>
      <section className="examples" aria-label="演示案例">
        <h2>演示案例</h2>
        {examples.map(item => <button type="button" key={item.id} className="example" onClick={() => chooseExample(item.id)}
          disabled={!!pending || !demo} aria-pressed={example === item.id && page === 'chat'}>
          <item.icon size={16} /><span><strong>{item.title}</strong><small>{item.description}</small></span>
        </button>)}
      </section>
      <div className="sidebar-footer">
        <a href="/docs" target="_blank" rel="noreferrer"><span>API 文档</span><ArrowUpRight size={16} /></a>
        <a href="https://github.com/Google-Health/consumer-health-research/tree/3e9d1e4877a54cf81ce8ef34085006952dd19601/personal_health_agent/data/sample"
          target="_blank" rel="noreferrer"><span>演示数据来源</span><ArrowUpRight size={16} /></a>
      </div>
    </aside>
    <main>
      <header className="topbar">
        <span><Activity size={15} /> PersonalHealthAgent</span>
        <div><span className="data-tag"><Database size={13} />公开合成数据</span>{demo && <span className="desktop-only">截至 {demo.dataset.as_of}</span>}</div>
      </header>
      <div className="main-content">
        <header className="page-heading">
          <div>
            <h1>{page === 'chat' ? '对话工作台' : 'Skill 自进化'}</h1>
            <p>{page === 'chat' ? '看清一次提问如何变成分析、证据和下一步行动。' : '从一次漏检到一个通过评测的新版本。'}</p>
          </div>
          {page === 'chat' && demo && <button className="secondary-button" onClick={() => chooseExample('joint')} disabled={!!pending}><RotateCcw size={15} />重置演示</button>}
        </header>
        {page === 'chat' ? <>
          <section className="overview" aria-label="当前运行概览">
            <div className="overview-question">
              <span className="eyeline"><Activity size={15} />{turn?.mode === 'live' ? '本轮提问' : '三 Agent 协作回放'}</span>
              <h2>{turn?.mode === 'live' ? turn.question : '走得多的日子，深睡更长吗？'}</h2>
              <p>{turn?.mode === 'live' ? '查看本轮实际执行的工作流和 Skill。' : '回放一次真实执行，查看步数与睡眠的配对和证据核查。'}</p>
            </div>
            <div className="overview-facts">
              <div><span>{analysis?.result ? '有效配对日' : '公开数据天数'}</span><strong>{analysis?.result?.paired_days ?? demo?.dataset.days ?? '—'}<small> 天</small></strong></div>
              <div><span>本轮执行 Agent</span><strong>{roles.length}<small> 个</small></strong></div>
            </div>
          </section>
          {loadError && <div className="inline-error" role="alert">{loadError} <button onClick={() => { setLoadError(''); setLoadAttempt(value => value + 1) }}>重新加载</button></div>}
          <div className="workbench">
            <section className="chat-panel panel" aria-label="对话">
              <div className="panel-heading">
                <span className="heading-icon"><MessageSquare size={18} /></span>
                <div><h2>演示会话</h2><span>{turn?.mode === 'live' ? '同一会话保留目标与行动' : '可浏览已录制的完整运行'}</span></div>
                <span className={'pill ' + (turn?.mode === 'live' ? 'green' : 'neutral')}>{turn?.mode === 'live' ? '实时执行' : '案例回放'}</span>
              </div>
              <div className="messages" ref={messagesRef} aria-live="polite">
                {!messages.length && !loadError && <div className="loading"><LoaderCircle size={18} /> 正在加载演示</div>}
                {messages.map((item, index) => <div className="exchange" key={index}>
                  <p className="user-message">{item.question}</p>
                  <div className="agent-message"><span className="assistant-mark"><HeartPulse size={17} /></span><div><strong className="assistant-name">健康 Agent</strong><Reply turn={item} /></div></div>
                </div>)}
                {pending && <div className="exchange"><p className="user-message">{pending}</p><div className="loading" role="status"><LoaderCircle size={18} />
                  {progress?.agent ? roleNames[progress.agent] + (progress.phase === 'completed' ? '已完成' : '正在准备或执行') : '正在编排'}
                </div></div>}
              </div>
              <div className="composer-area">
                {chatError && <p className="inline-error" role="alert">{chatError}</p>}
                {hasLiveJoint && turn?.mode === 'replay' && <button className="run-live" onClick={() => { setExample('joint'); void send(demo.replay.question) }} disabled={!!pending}><Play size={14} />实时运行该案例</button>}
                <form className="composer" onSubmit={submit}>
                  <input aria-label="提问" maxLength={1200} value={message} onChange={event => setMessage(event.target.value)}
                    placeholder="问问步数、睡眠或你的行动计划…" />
                  <button type="submit" aria-label="发送" disabled={!message.trim() || !!pending || !demo}><Send size={16} /></button>
                </form>
                <div className="composer-caption"><Database size={13} /> {hasLiveJoint ? '实时联合分析可用' : '实时支持查询、本地 RAG 和教练'} <span>· {demo?.capabilities.online_evidence ? '在线证据开启' : '本地演示'}</span></div>
              </div>
            </section>
            <TracePanel key={history.length} turn={turn} busy={!!pending} progress={progress} />
          </div>
        </> : evolution ? <EvolutionPanel data={evolution} /> : evolutionError ?
          <div className="inline-error" role="alert">{evolutionError} <button onClick={() => { setEvolutionError(''); setEvolutionAttempt(n => n + 1) }}>重新加载</button></div> :
          <div className="loading"><LoaderCircle size={18} /> 正在读取评测报告</div>}
        <footer className="page-footer"><Layers3 size={14} /> PersonalHealthAgent<span>Python · FastAPI · React</span></footer>
      </div>
    </main>
  </div>
}

createRoot(document.getElementById('root')!).render(<App />)
