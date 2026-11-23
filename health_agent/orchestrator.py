import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, timedelta

from .agents import Analysis, DomainExpertAgent, HealthCoachAgent, Interpretation
from .coaching import CoachingLoop, coaching_route
from .collaboration import AgentRole, Assignment, Collaboration, Workflow, WorkflowPlanner
from .domain import CoachMemory, Intent, Metric, Route, Task, TraceEvent, Window
from .ds_analysis import SandboxAnalysis, VerifiedComparison, comparison_arguments
from .evidence import EvidenceReport, EvidenceResearch, EvidenceScope, asks_public_evidence
from .model import IntentModel, Ollama, ToolCall, ToolModel, WorkflowModel
from .reflection import GlobalReflector, ReflectionQuestion, ReflectionRun
from .router import Router
from .sdk import HealthTools, metric_arguments
from .skills import ActiveSkill, SkillEvent, SkillRegistry
from .store import Store


@dataclass(frozen=True)
class Reflection:
    metric: Metric
    requested_window: Window
    fallback_window: Window


@dataclass(frozen=True)
class ToolInvocation:
    name: str
    arguments: dict
    source: str


@dataclass(frozen=True)
class CoachingTurn:
    metric: Metric
    action: str
    draft: str
    response: str
    memory: CoachMemory


@dataclass(frozen=True)
class RunProgress:
    phase: str
    agent: AgentRole | None = None
    operation: str = ""


@dataclass(frozen=True)
class Turn:
    reply: str
    route: Route
    trace: tuple[TraceEvent, ...]
    workflow: Workflow
    reflection: Reflection | None = None
    sandbox_analyses: tuple[SandboxAnalysis, ...] = ()
    tool_calls: tuple[ToolInvocation, ...] = ()
    evidence_runs: tuple[EvidenceReport, ...] = ()
    coaching_turns: tuple[CoachingTurn, ...] = ()
    global_reflections: tuple[ReflectionRun, ...] = ()
    skill_events: tuple[SkillEvent, ...] = ()

    @property
    def collaborations(self) -> tuple[Collaboration, ...]:
        return tuple(item.collaboration for item in self.workflow.items)


class Orchestrator:
    def __init__(self, store: Store, model: Ollama | None = None, *,
                 intent_model: IntentModel | None = None, workflow_model: WorkflowModel | None = None,
                 ds_model: WorkflowModel | None = None, tool_model: ToolModel | None = None,
                 evidence: EvidenceResearch | None = None, coach_loop: bool = False,
                 coach_model: WorkflowModel | None = None, skills: SkillRegistry | None = None):
        self.store = store
        self.router = Router(intent_model if intent_model is not None else model, evidence=evidence is not None,
                             coaching=coach_loop)
        self.workflow = WorkflowPlanner(workflow_model if workflow_model is not None else model, evidence=evidence is not None)
        self.tools = HealthTools(store, ds_model=ds_model)
        self.tool_model = tool_model
        self.de = DomainExpertAgent(model, evidence)
        self.hc = HealthCoachAgent(model)
        coaching_model = coach_model or workflow_model or model
        self.coaching = CoachingLoop(coaching_model) if coach_loop else None
        self.reflector = GlobalReflector(coaching_model)
        self.skills = skills

    def run(self, message: str, session: str, today: date, *,
            on_progress: Callable[[RunProgress], None] | None = None) -> Turn:
        if not message.strip():
            raise ValueError("请输入一个问题。")
        context = self.store.context(session)
        route = self.router.plan(message.strip(), context, today)
        if self.coaching is not None:
            route = coaching_route(route, message, context)
        skills = self.skills.start() if self.skills else None
        workflow = self.workflow.plan(message, context, route, skills=self.skills.metadata if self.skills else ())
        if on_progress:
            on_progress(RunProgress("planned", workflow.main_agent))
        goals = dict(context.goals)
        updates: dict[Metric, float] = {}
        analyses: dict[tuple[str, Metric, tuple[Window, ...], bool, float | None], Analysis] = {}
        last_metrics = set()
        paragraphs = []
        trace = []
        reflection = None
        sandbox_analyses = []
        tool_calls = []
        evidence_runs = []
        coaching_turns = []
        global_reflections = []
        remembered_coaching = {memory.metric: memory for memory in context.coaching}
        coaching_updates = {}

        def record(event: TraceEvent) -> None:
            trace.append(event)
            if on_progress:
                on_progress(RunProgress("completed", event.agent, event.operation))

        def activate(role: AgentRole) -> ActiveSkill | None:
            if on_progress:
                on_progress(RunProgress("started", role))
            return skills.activate(role.value) if skills else None

        def research(scope: EvidenceScope, observation: str = "", analysis: VerifiedComparison | None = None):
            interpretation = self.de.research(scope, observation, skill=activate(AgentRole.DOMAIN_EXPERT),
                                              personal_analysis=analysis)
            evidence_runs.append(interpretation.evidence)
            paragraphs.append(interpretation.text)
            return interpretation

        def execute(name: str, arguments: dict, subquery: str, *, question: str = "", propose: bool = True):
            skill = activate(AgentRole.DATA_SCIENCE)
            call = ToolCall(name, arguments)
            source = "workflow"
            if self.tool_model is not None and propose:
                approved = {"task": subquery, "approved_arguments": arguments}
                proposed = self.tool_model.tool_calls(
                    "Invoke the provided tool exactly once. Copy the approved_arguments without changing any field.",
                    approved, [self.tools.function(name)],
                )
                if proposed == (call,):
                    call, = proposed
                    source = "model"
                else:
                    source = "workflow_fallback"
            result = self.tools.execute(call.name, call.arguments, goals=goals, question=question, subquery=subquery,
                                        skill=skill)
            tool_calls.append(ToolInvocation(call.name, call.arguments, source))
            return result

        def coach(task: Task, step: Assignment, metric: Metric, interpretation: Interpretation | None,
                  observation: str = "") -> None:
            memory = coaching_updates.get(metric, remembered_coaching.get(metric))
            known = {"personal_observation": observation}
            if interpretation is not None:
                known["domain_context"] = (interpretation.evidence.coach_context() if interpretation.evidence
                                           else interpretation.text)
            draft = self.coaching.draft(message, metric, memory, observation, skill=activate(step.agent),
                                        domain_context=known.get("domain_context") if skills else None)
            actual = draft
            allowed = []
            window = task.windows[-1] if task.windows else Window(today - timedelta(days=6), today)
            if draft.missing_fact == "bedtime" and not re.search(r"(?:别|不要|不用|不需要).{0,6}(?:查|读取|用).{0,8}(?:记录|数据)", message):
                allowed.append(ReflectionQuestion("DS", "bedtime", f"查询 {window} 的入睡时刻和记录覆盖。"))
            if (self.de.evidence and asks_public_evidence(message) and not (interpretation and interpretation.evidence)
                    and not re.search(r"(?:别|不要|不用|不需要).{0,10}(?:查|用)?(?:研究|文献|证据)", message)
                    and draft.action not in {"agree", "close"}):
                allowed.append(ReflectionQuestion("DE", "evidence", f"核查与{metric.label}相关的证据和适用范围。"))

            review = self.reflector.check(draft.reply, draft.action, known, tuple(allowed))
            findings = {}
            supporting = list(step.depends_on)
            for question in review.reflection_questions:
                if question.fact == "bedtime":
                    activate(AgentRole.DATA_SCIENCE)
                    timing = self.tools.ds.bedtime_summary(window)
                    findings["bedtime"] = timing or "所选窗口没有可核实的入睡时刻。"
                    actual = self.coaching.revise_bedtime(actual, timing)
                    record(TraceEvent(AgentRole.DATA_SCIENCE, "reflect_bedtime", metric.value))
                else:
                    scope = EvidenceScope.from_question(metric, message, joint=bool(task.analysis_question))
                    answer = research(scope, observation)
                    findings["evidence"] = answer.text
                    interpretation = answer
                    record(TraceEvent(AgentRole.DOMAIN_EXPERT, "reflect_evidence", metric.value,
                                      used_model=answer.used_model))
                supporting.append(AgentRole(question.agent))
            if interpretation and interpretation.evidence:
                report = interpretation.evidence
                sources = [f"{item.title}（{item.url}）" for item in report.findings]
                evidence_memory = "\n".join(sources + list(report.limitations))[:1600]
                actual = replace(actual, memory=replace(actual.memory, last_evidence=evidence_memory))
            global_reflections.append(ReflectionRun(review.decision, review.reflection_questions, review.source,
                                                    findings, actual.reply != draft.reply or "evidence" in findings))
            coaching_updates[metric] = actual.memory
            coaching_turns.append(CoachingTurn(metric, actual.action, draft.reply, actual.reply, actual.memory))
            paragraphs.append(actual.reply)
            record(TraceEvent(AgentRole.HEALTH_COACH, actual.action, metric.value,
                              tuple(dict.fromkeys(supporting)), actual.used_model))

        for work in workflow.items:
            task, collaboration = work.task, work.collaboration
            metric = collaboration.metric
            if task.analysis_question:
                last_metrics.update(task.metrics)
                query, *downstream = collaboration.assignments
                if self.tools.sandbox is None:
                    joint = SandboxAnalysis("unavailable", "步数与睡眠的配对分析需要启用 DS 沙箱。")
                else:
                    try:
                        arguments = comparison_arguments(task)
                    except ValueError as error:
                        joint = SandboxAnalysis("unsupported", str(error))
                    else:
                        joint = execute("analyze_steps_sleep", arguments, query.subquery,
                                        question=task.analysis_question)
                    sandbox_analyses.append(joint)
                    record(TraceEvent(AgentRole.DATA_SCIENCE, "sandbox_analysis", "steps+sleep",
                                      used_model=joint.plan is not None))
                paragraphs.append(f"个人记录\n{joint.reply}" if downstream and joint.status == "ok" else joint.reply)
                interpretation = None
                for step in downstream:
                    if step.agent is AgentRole.DOMAIN_EXPERT:
                        scope = EvidenceScope.from_question(Metric.SLEEP, task.analysis_question, joint=True,
                                                            outcome=joint.plan.sleep_metric if joint.plan else None,
                                                            alignment=joint.plan.alignment if joint.plan else "bedtime_day")
                        interpretation = research(scope, joint.reply if joint.status == "ok" else "",
                                                  joint.verified_comparison if skills else None)
                        record(TraceEvent(step.agent, "retrieve_evidence", "steps+sleep",
                                          step.depends_on, interpretation.used_model))
                    else:
                        if self.coaching is not None:
                            coach(task, step, Metric.SLEEP, interpretation, joint.reply if joint.status == "ok" else "")
                        else:
                            coaching = self.hc.plan(Metric.SLEEP, None, interpretation, goals.get(Metric.SLEEP),
                                                    step.subquery, skill=activate(step.agent))
                            paragraphs.append(coaching.text)
                            record(TraceEvent(step.agent, "plan", "steps+sleep", step.depends_on, coaching.used_model))
                continue
            last_metrics.add(metric)
            analysis = None
            interpretation = None
            for step in collaboration.assignments:
                if step.agent is AgentRole.DATA_SCIENCE:
                    name = "review_goal" if task.intent is Intent.REVIEW else "query_metric"
                    target = goals.get(metric) if name == "review_goal" else None
                    key = (name, metric, task.windows, task.explicit_window, target)
                    analysis = analyses.get(key)
                    if analysis is None:
                        analysis = execute(name, metric_arguments(metric, task.windows), step.subquery)
                        record(TraceEvent(step.agent, "measure", metric.value))
                        if reflection is None and not task.explicit_window and not any(item.samples for item in analysis.stats):
                            window, = task.windows
                            latest = self.store.last_observation(metric, window.end)
                            if latest and latest < window.start:
                                start = date.fromordinal(max(1, latest.toordinal() - window.days + 1))
                                fallback = Window(start, latest)
                                analysis = execute(name, metric_arguments(metric, (fallback,)), step.subquery, propose=False)
                                reflection = Reflection(metric, window, fallback)
                                record(TraceEvent(step.agent, "measure_latest_available", metric.value))
                                paragraphs.append(f"{window}没有{metric.label}记录，以下使用最近可用的记录窗口。")
                        analyses[key] = analysis
                    description = analysis.describe(task.show_trend)
                    if description not in paragraphs:
                        paragraphs.append(description)
                elif step.agent is AgentRole.DOMAIN_EXPERT:
                    if task.evidence_question:
                        scope = EvidenceScope.from_question(metric, task.evidence_question, joint=task.evidence_joint)
                        interpretation = research(scope, analysis.describe() if analysis else "")
                        operation = "retrieve_evidence"
                    else:
                        interpretation = self.de.run(metric, analysis, step.subquery, skill=activate(step.agent))
                        operation = "interpret"
                        if interpretation.text not in paragraphs:
                            paragraphs.append(interpretation.text)
                    record(TraceEvent(step.agent, operation, metric.value, step.depends_on, interpretation.used_model))
                elif task.intent is Intent.SET_GOAL:
                    activate(step.agent)
                    if task.target is None:
                        raise ValueError("设置目标需要已经校验的数值。")
                    goals[metric] = updates[metric] = task.target
                    record(TraceEvent(step.agent, "set_goal", metric.value))
                    paragraphs.append(f"已保存{metric.label}目标：每天 {task.target:g} {metric.unit}。")
                else:
                    if self.coaching is not None and task.intent is Intent.PLAN:
                        observed = analysis.describe() if analysis and any(row.samples for row in analysis.stats) else ""
                        coach(task, step, metric, interpretation, observed)
                        continue
                    skill = activate(step.agent)
                    coaching = (
                        self.hc.review(analysis, interpretation) if task.intent is Intent.REVIEW
                        else self.hc.plan(metric, analysis, interpretation, goals.get(metric), step.subquery, skill=skill)
                    )
                    record(TraceEvent(step.agent, task.intent.value, metric.value, step.depends_on, coaching.used_model))
                    paragraphs.append(coaching.text)

        if route.clarification:
            paragraphs.append(route.clarification)
        reply = "\n\n".join(paragraphs)
        remembered_metric = next(iter(last_metrics)) if len(last_metrics) == 1 else None
        if not last_metrics:
            remembered_metric = context.last_metric
        self.store.finish_turn(session, message, reply, remembered_metric, route.pending_message, updates,
                               coaching=coaching_updates)
        return Turn(reply, route, tuple(trace), workflow, reflection, tuple(sandbox_analyses),
                    tuple(tool_calls), tuple(evidence_runs), tuple(coaching_turns), tuple(global_reflections),
                    tuple(skills.events) if skills else ())
