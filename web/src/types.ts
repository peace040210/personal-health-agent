export type Role = 'DS' | 'DE' | 'HC'
export type Fit = 'direct' | 'context' | 'insufficient' | 'population'

export interface Progress {
  phase: 'planned' | 'started' | 'completed'
  agent: Role | null
  operation: string
}

export interface TraceStep {
  agent: Role
  operation: string
  metric: string
  received_from: Role[]
  used_model: boolean
}

export interface Assignment {
  agent: Role
  subquery: string
  depends_on: Role[]
}

export interface SkillEvent {
  role: Role
  version: string
  stage: 'discover' | 'activate' | 'reference'
  resource: string
}

export interface Sandbox {
  status: string
  reply: string
  plan: { start: string; end: string; alignment: string; sleep_metric: string; threshold: string } | null
  attempts: { number: number; status: string; feedback: string }[]
  result: {
    paired_days: number
    step_cutoff: number
    high_days: number
    other_days: number
    high_mean_hours: number
    other_mean_hours: number
    difference_hours: number
  } | null
}

export interface Search {
  round: number
  tool: string
  query: string
  status: string
  found: number
}

export interface Finding {
  source: string
  title: string
  url: string
  fit: Fit
  quote: string
  scope_notes: string[]
}

export interface Evidence {
  investigations: Search[]
  findings: Finding[]
  limitations: string[]
}

export interface Turn {
  mode: 'replay' | 'live'
  question: string
  reply: string
  details: {
    workflow: { main_agent: Role | null; supporting_agents: Role[]; assignment_source: string; subquery_source: string }
    collaboration_plan: { assignments: Assignment[] }[]
    trace: TraceStep[]
    sandbox_analyses: Sandbox[]
    evidence_runs: Evidence[]
    tool_calls: { name: string; arguments: Record<string, unknown>; source: string }[]
    coaching_turns: { metric: string; action: string; response: string; memory: { objective: string; agreed_action: string } }[]
    global_reflections: { decision: string; findings: Record<string, string>; revised: boolean }[]
    skill_events: SkillEvent[]
  }
}

export interface Demo {
  dataset: { days: number; as_of: string; synthetic: boolean }
  capabilities: { joint_live: boolean; model: boolean; online_evidence: boolean }
  replay: Turn
}

export type Split = 'dev' | 'val' | 'test'
export interface Score { passed: number; total: number; families: number; guard_failures: number; search_calls: number }
export interface Probe {
  family: string
  question: string
  expected: Fit
  actual: Fit
  passed: boolean
  split?: Split
  searches: Search[]
}
export interface Candidate {
  version: string
  status: string
  policy: { joint_search_order: string }
  rationale: string
  dev: Score
  val: Score
  val_regressions: number
  cases: Record<string, Probe>
  regressions: string[]
}
export interface Evolution {
  run: string
  version: string
  baseline: Record<Split, Score>
  final: Record<Split, Score>
  cases: Record<string, Probe>
  candidates: Candidate[]
  lessons: { what_changed: string; recovered_families: string[] }[]
}
