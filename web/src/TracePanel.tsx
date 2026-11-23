import { useState } from 'react'
import { Activity, BookOpen, Check, ChevronDown, CodeXml, ExternalLink, HeartHandshake, Layers3, ListTree, Workflow } from 'lucide-react'
import type { Evidence, Progress, Role, Sandbox, Turn } from './types'

export const roleNames = { DS: '数据分析', DE: '证据检索', HC: '健康教练' }
const roleIcons = { DS: Activity, DE: BookOpen, HC: HeartHandshake }
const operations: Record<string, string> = {
  sandbox_analysis: '数据配对与复算', measure: '查询日记录', measure_latest_available: '使用最近记录',
  retrieve_evidence: '检索并核查证据', interpret: '解释统计口径', clarify: '澄清下一步',
  plan: '制定行动计划', propose: '提出行动建议', review: '复盘行动', agree: '确认计划',
  close: '结束本轮教练', set_goal: '保存目标', reflect_bedtime: '补查入睡记录', reflect_evidence: '补查外部证据',
}
const attemptNames: Record<string, string> = {
  execution_error: '执行失败', result_error: '复算不符', generation_error: '代码生成失败',
  unavailable: '执行不可用', code_update: '审查要求修改', verified: '通过独立复算',
}
export const fitNames = { direct: '直接匹配', context: '背景资料', insufficient: '证据不足', population: '群体统计' }
export const sourceNames: Record<string, string> = {
  pubmed: 'PubMed', local_kb: '本地知识库', medlineplus: 'MedlinePlus', tavily: 'Tavily', data_commons: 'Data Commons',
}

export function RoleMark({ role }: { role: Role }) {
  const Icon = roleIcons[role]
  return <span className={'role-mark ' + role.toLowerCase()}><Icon size={17} /></span>
}

function SandboxDetails({ analysis }: { analysis: Sandbox }) {
  const { plan, result, attempts } = analysis
  return <>
    {result ? <>
      <div className="analysis-numbers">
        <div><strong>{result.paired_days}</strong><span>有效配对日</span></div>
        <div><strong>{result.step_cutoff.toLocaleString('zh-CN')}</strong><span>分组阈值 · 步</span></div>
      </div>
      <div className="bar-chart" aria-label="两组平均睡眠时长，从零小时起">
        {[
          { title: '高步数组', value: result.high_mean_hours, days: result.high_days },
          { title: '其余组', value: result.other_mean_hours, days: result.other_days },
        ].map((group, index) => {
          const max = Math.max(result.high_mean_hours, result.other_mean_hours, 0.1)
          return <div className="bar-row" key={group.title}>
            <div><span>{group.title} <small>n={group.days}</small></span><strong>{group.value.toFixed(2)} h</strong></div>
            <span className="bar-track"><span className={index ? 'muted-bar' : ''} style={{ width: (group.value / max * 100) + '%' }} /></span>
          </div>
        })}
      </div>
    </> : <p className="subtle">{analysis.reply}</p>}
    {plan && <p className="method-line">{plan.start} – {plan.end} · {plan.alignment === 'bedtime_day' ? '按入睡日配对' : '按记录日配对'}</p>}
    {attempts.length > 0 && <div className="attempts">
      {attempts.map(attempt => <div key={attempt.number}>
        <span aria-hidden="true" className={'attempt-dot ' + (attempt.status === 'verified' ? 'success' : 'warning')} />
        <span>第 {attempt.number} 次 · {attemptNames[attempt.status] ?? attempt.status}</span>
        {attempt.feedback && <details><summary>反馈</summary><p>{attempt.feedback}</p></details>}
      </div>)}
    </div>}
  </>
}

function EvidenceDetails({ evidence }: { evidence: Evidence }) {
  return <>
    {evidence.investigations.length > 0 && <div className="tool-strip">
      {evidence.investigations.map((item, index) => <span key={index}>{sourceNames[item.tool] ?? item.tool} <b>{item.found}</b></span>)}
    </div>}
    {evidence.findings.map((finding, index) => <details className="citation" key={finding.url + index}>
      <summary><span>{finding.title}</span><ChevronDown size={13} /></summary>
      <div>
        <span className={'pill ' + (finding.fit === 'direct' ? 'green' : 'neutral')}>{fitNames[finding.fit]}</span>
        {finding.quote && <blockquote>{finding.quote}</blockquote>}
        {finding.scope_notes.length > 0 && <p className="subtle">{finding.scope_notes.join(' · ')}</p>}
        {finding.url.startsWith('https://') && <a href={finding.url} target="_blank" rel="noreferrer">查看来源 <ExternalLink size={12} /></a>}
      </div>
    </details>)}
    {evidence.limitations.map(note => <p className="method-line" key={note}>{note}</p>)}
  </>
}

export function TracePanel({ turn, busy, progress }: { turn: Turn | null; busy: boolean; progress: Progress | null }) {
  const [tab, setTab] = useState<'trace' | 'skills'>('trace')
  const [openIndex, setOpenIndex] = useState(0)
  const details = turn?.details
  const roles = details ? [...new Set(details.trace.map(step => step.agent))] : []
  const active = details?.skill_events.filter(item => item.stage === 'activate') ?? []
  return <aside className="trace-panel panel" aria-label="执行轨迹">
    <div className="panel-heading">
      <span className="heading-icon"><Workflow size={18} /></span>
      <div><h2>执行轨迹</h2><span>编排器实际返回的步骤</span></div>
      <span className="count-label">{details?.trace.length ?? 0} 步</span>
    </div>
    <div className="trace-tabs" aria-label="轨迹视图">
      <button type="button" aria-pressed={tab === 'trace'} onClick={() => setTab('trace')}><ListTree size={15} />工作流</button>
      <button type="button" aria-pressed={tab === 'skills'} onClick={() => setTab('skills')}><Layers3 size={15} />Skill 加载</button>
    </div>
    {busy && <p className="trace-wait" role="status">{progress?.agent && progress.phase !== 'planned'
      ? progress.agent + ' ' + (progress.phase === 'completed' ? '已完成' : '正在执行') + '。'
      : '正在确定本轮工作流。'}下方显示上一轮轨迹。</p>}
    {details && tab === 'trace' && <>
      {roles.length > 0 && <div className="trace-summary"><span>主 Agent <b>{details.workflow.main_agent ?? '—'}</b></span><span>{details.workflow.assignment_source === 'model' ? '模型编排' : '规则编排'}</span></div>}
      <div className="step-list">
        {details.trace.map((step, index) => {
          const expanded = openIndex === index
          const preceding = details.trace.slice(0, index)
          const isEvidence = (operation: string) => ['retrieve_evidence', 'reflect_evidence'].includes(operation)
          const analysis = step.operation === 'sandbox_analysis'
            ? details.sandbox_analyses[preceding.filter(item => item.operation === 'sandbox_analysis').length] : undefined
          const evidence = isEvidence(step.operation)
            ? details.evidence_runs[preceding.filter(item => isEvidence(item.operation)).length] : undefined
          const coaching = step.agent === 'HC' ? details.coaching_turns
            .filter(item => item.action === step.operation && item.metric === step.metric)
            [preceding.filter(item => item.agent === 'HC' && item.operation === step.operation && item.metric === step.metric).length] : undefined
          const task = details.collaboration_plan.flatMap(plan => plan.assignments).find(item => item.agent === step.agent)
          return <section className={'trace-step ' + (expanded ? 'expanded' : '')} key={index}>
            <button aria-expanded={expanded} aria-controls={'trace-step-' + index} onClick={() => setOpenIndex(expanded ? -1 : index)}>
              <RoleMark role={step.agent} />
              <span><strong>{step.agent} · {roleNames[step.agent]}</strong><small>{operations[step.operation] ?? step.operation}</small></span>
              <ChevronDown size={16} className="step-chevron" />
            </button>
            {expanded && <div id={'trace-step-' + index} className="step-body">
              {step.received_from.length > 0 && <p className="method-line">收到 {step.received_from.join('、')} 的结果</p>}
              {analysis ? <SandboxDetails analysis={analysis} /> : evidence ? <EvidenceDetails evidence={evidence} />
                : coaching ? <p className="subtle">{coaching.response}</p>
                  : <p className="subtle">{task?.subquery ?? operations[step.operation]}</p>}
            </div>}
          </section>
        })}
        {!details.trace.length && <div className="trace-empty"><ListTree size={22} /><p>这一轮等待澄清后再派发任务。</p></div>}
      </div>
      {details.tool_calls.length > 0 && <details className="tool-details">
        <summary><CodeXml size={16} />工具调用 <span>{details.tool_calls.length}</span><ChevronDown size={14} /></summary>
        {details.tool_calls.map((tool, index) => <div key={index}><code>{tool.name}</code><span>{tool.source === 'model' ? '模型发出' : '编排器执行'}</span><pre>{JSON.stringify(tool.arguments, null, 2)}</pre></div>)}
      </details>}
    </>}
    {details && tab === 'skills' && <div className="skill-disclosure">
      <p className="subtle">{active.length} 个 Skill 已激活。编排器先发现元数据，再按需读取正文和资料。</p>
      {details.skill_events.map((item, index) => <div className="skill-event" key={index}>
        <RoleMark role={item.role} />
        <span><strong>{item.role} · {{ discover: '发现元数据', activate: '加载正文', reference: '读取资料' }[item.stage]}</strong>
          <small>{item.resource || item.version}</small></span>
        {item.stage !== 'discover' && <Check size={15} />}
      </div>)}
    </div>}
    {!details && <div className="trace-empty"><Workflow size={24} /><p>运行一个案例后查看执行步骤。</p></div>}
  </aside>
}
