import { useState } from 'react'
import { ArrowDown, ArrowRight, Check, CircleAlert, FlaskConical, GitBranch, History, ListChecks, Search } from 'lucide-react'
import { fitNames, sourceNames } from './TracePanel'
import type { Candidate, Evolution, Probe, Split } from './types'

const splitNames: Record<Split, string> = { dev: '开发集', val: '验证集', test: '留出集' }
const strategyNames: Record<string, string> = {
  'retry-without-timing': '保留指标，放宽检索时序',
  'metrics-first': '首轮优先检索指标',
  legacy: '先查指标和时序，再扩大活动范围',
}

function ProbeResult({ title, probe }: { title: string; probe?: Probe }) {
  return <div className="probe-result">
    <div className="probe-heading"><strong>{title}</strong>{probe && <span className={'pill ' + (probe.passed ? 'green' : 'amber')}>{probe.passed ? '通过' : '未通过'}</span>}</div>
    {probe ? <>
      <div className="verdict"><span>实际</span><strong>{fitNames[probe.actual]}</strong></div>
      <ol className="search-log">
        {probe.searches.map((item, index) => <li key={index}>
          <details><summary><Search size={13} />{sourceNames[item.tool] ?? item.tool}<span>{item.found} 条</span></summary>
            <code>{item.query}</code>
          </details>
        </li>)}
      </ol>
    </> : <p className="subtle">该候选没有在这一数据切分上复测。</p>}
  </div>
}

export function EvolutionPanel({ data }: { data: Evolution }) {
  const [chosenVersion, setChosenVersion] = useState(data.version)
  const candidate = data.candidates.find(value => value.version === chosenVersion)
  const usefulFamily = (choice: Candidate | undefined) => {
    const regressed = Object.values(choice?.cases ?? {}).find(row => !row.passed && data.cases[row.family]?.passed)
    return regressed?.family ?? data.lessons[0]?.recovered_families[0] ?? Object.keys(data.cases)[0]
  }
  const [family, setFamily] = useState(() => usefulFamily(candidate))
  const base = data.cases[family]
  const selected = candidate?.cases[family]

  return <div className="evolution-view">
    <section className="evolution-banner">
      <span className="banner-icon"><FlaskConical size={25} /></span>
      <div><h2>DE 检索策略回放</h2><p>按场景隔离合成数据，由模型提出候选，并用固定检索结果复测。</p></div>
      <span className="pill green">已通过留出评测</span>
    </section>
    <div className="score-grid">
      {(Object.keys(splitNames) as Split[]).map(split => {
        const before = data.baseline[split], after = data.final[split]
        return <section className="score-card panel" key={split}>
          <span>{splitNames[split]}</span>
          <div><span>{before.passed}</span><ArrowRight size={20} /><strong>{after.passed}<small>/{after.total}</small></strong></div>
          <p>{after.families} 个合成场景 · {after.guard_failures} 条边界校验失败</p>
        </section>
      })}
    </div>
    <div className="evolution-grid">
      <section className="panel candidate-panel">
        <div className="panel-heading"><span className="heading-icon"><GitBranch size={18} /></span><div><h2>候选策略</h2><span>选择版本以检查命中与回退</span></div></div>
        <div className="candidates">
          {data.candidates.map(choice => {
            const adopted = choice.version === data.version
            return <button type="button" className="candidate" key={choice.version} aria-pressed={choice.version === chosenVersion}
              onClick={() => { setChosenVersion(choice.version); setFamily(usefulFamily(choice)) }}>
              <span className={'candidate-symbol ' + (adopted ? 'accepted' : 'rejected')}>{adopted ? <Check size={16} /> : <CircleAlert size={16} />}</span>
              <span><strong>{strategyNames[choice.policy.joint_search_order] ?? choice.policy.joint_search_order}</strong>
                <small>开发 {choice.dev.passed}/{choice.dev.total} · 验证 {choice.val.passed}/{choice.val.total}</small></span>
              <span className={'pill ' + (adopted ? 'green' : 'amber')}>{adopted ? '入选' : '淘汰'}</span>
            </button>
          })}
        </div>
        {candidate && <div className="rationale">
          <span>候选归因</span>
          <p>{candidate.rationale}</p>
          <p className="muted-copy">{candidate.val_regressions} 条验证回退</p>
        </div>}
      </section>
      <section className="panel comparison-panel">
        <div className="panel-heading"><span className="heading-icon"><ListChecks size={18} /></span><div><h2>逐场景对照</h2><span>查看同一问题触发的检索</span></div></div>
        <div className="comparison-body">
          <label htmlFor="probe-family">评测场景</label>
          <select id="probe-family" value={family} onChange={event => setFamily(event.target.value)}>
            {Object.values(data.cases).map(probe => <option key={probe.family} value={probe.family}>
              {probe.split ? splitNames[probe.split] + ' · ' : ''}{probe.question}
            </option>)}
          </select>
          {base && <p className="expected-line">预期 <strong>{fitNames[base.expected]}</strong></p>}
          <div className="probe-grid">
            <ProbeResult title="初始策略" probe={base} />
            <span className="probes-arrow"><ArrowRight size={17} /><ArrowDown size={17} /></span>
            <ProbeResult title="所选候选" probe={selected} />
          </div>
        </div>
      </section>
    </div>
    {data.lessons[0] && <section className="lesson-card panel">
      <span className="heading-icon"><History size={18} /></span>
      <div><h2>留存的经验</h2><p>{data.lessons[0].what_changed}</p><code>{data.version}</code></div>
    </section>}
  </div>
}
