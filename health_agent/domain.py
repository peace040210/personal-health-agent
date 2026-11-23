from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class Metric(StrEnum):
    SLEEP = "sleep_hours"
    STEPS = "steps"

    @property
    def label(self) -> str:
        return {Metric.SLEEP: "睡眠时长", Metric.STEPS: "步数"}[self]

    @property
    def unit(self) -> str:
        return {Metric.SLEEP: "小时", Metric.STEPS: "步"}[self]

    def valid(self, value: float) -> bool:
        if self is Metric.SLEEP:
            return 0 <= value <= 24
        return 0 <= value < 2**63 and int(value) == value


class Intent(StrEnum):
    ANALYZE = "analyze"
    EXPLAIN = "explain"
    PLAN = "plan"
    REVIEW = "review_goal"
    SET_GOAL = "set_goal"


class Journey(StrEnum):
    KNOWLEDGE = "general_health_knowledge"
    PERSONAL_DATA = "personal_data_interpretation"
    WELLNESS = "wellness_advice"
    SYMPTOMS = "symptom_assessment"


class Status(StrEnum):
    ACTIVE = "active"
    NEGATED = "negated"
    DEFERRED = "deferred"
    BACKGROUND = "background"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class Window:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def __str__(self) -> str:
        return f"{self.start} 至 {self.end}"


@dataclass(frozen=True)
class Task:
    intent: Intent
    status: Status
    clause: int
    metrics: tuple[Metric, ...] = ()
    windows: tuple[Window, ...] = ()
    target: float | None = None
    show_trend: bool = False
    explicit_window: bool = False
    analysis_question: str | None = None
    evidence_question: str | None = None
    evidence_joint: bool = False
    joint_coach: bool = False


@dataclass(frozen=True)
class Route:
    tasks: tuple[Task, ...] = ()
    candidates: tuple[Intent, ...] = ()
    clarification: str | None = None
    pending_message: str | None = None
    unsupported_journeys: tuple[Journey, ...] = ()


@dataclass(frozen=True)
class CoachMemory:
    metric: Metric
    objective: str = ""
    motivation: str = ""
    barrier: str = ""
    preference: str = ""
    proposal: str = ""
    agreed_action: str = ""
    pending: str = ""
    closed: bool = False
    last_observation: str = ""
    last_evidence: str = ""
    progress: str = ""


@dataclass(frozen=True)
class Context:
    last_metric: Metric | None
    pending_message: str | None
    recent: tuple[tuple[str, str], ...]
    goals: dict[Metric, float]
    coaching: tuple[CoachMemory, ...] = ()


@dataclass(frozen=True)
class TraceEvent:
    agent: str
    operation: str
    metric: str | None = None
    received_from: tuple[str, ...] = ()
    used_model: bool = False
