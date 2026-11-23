from dataclasses import dataclass
from statistics import linear_regression, mean, median

from .domain import Metric, Window
from .ds_analysis import VerifiedComparison
from .evidence import EvidenceReport, EvidenceResearch, EvidenceScope
from .model import Ollama
from .skills import ActiveSkill, skill_prompt
from .store import Store


@dataclass(frozen=True)
class Stats:
    window: Window
    samples: int
    average: float | None
    total: float
    met: int | None
    daily_trend: float | None


@dataclass(frozen=True)
class Analysis:
    metric: Metric
    stats: tuple[Stats, ...]
    target: float | None

    def describe(self, show_trend: bool = False) -> str:
        lines = []
        for item in self.stats:
            if item.average is None:
                lines.append(f"{item.window}：没有{self.metric.label}记录。")
                continue
            coverage = f"记录 {item.samples}/{item.window.days} 天"
            totals = f"，合计 {int(item.total):,} 步" if self.metric is Metric.STEPS else ""
            lines.append(f"{item.window}：{self.metric.label}平均 {item.average:.2f} {self.metric.unit}，{coverage}{totals}。")
            if show_trend:
                if item.daily_trend is None:
                    lines.append("趋势估计需要至少三个有记录的日期。")
                else:
                    lines.append(f"按有记录日期估算的线性趋势为每天 {item.daily_trend:+.2f} {self.metric.unit}。")
        observed = [item for item in self.stats if item.average is not None]
        if len(observed) == 2 and len(self.stats) == 2:
            earlier, later = observed
            difference = later.average - earlier.average
            if round(difference, 2) == 0:
                lines.append("两个窗口的日均值在显示精度内相同。")
            else:
                direction = "高" if difference > 0 else "低"
                lines.append(f"后一个窗口的日均值比前一个{direction} {abs(difference):.2f} {self.metric.unit}。")
        return "\n".join(lines)


class DataScienceAgent:
    def __init__(self, store: Store):
        self.store = store

    def run(self, metric: Metric, windows: tuple[Window, ...], target: float | None = None) -> Analysis:
        stats = []
        for window in windows:
            series = self.store.read_series(metric, window)
            values = [value for _, value in series]
            met = sum(value >= target for value in values) if target is not None else None
            trend = None
            if len(series) >= 3:
                offsets = [(day - window.start).days for day, _ in series]
                trend = linear_regression(offsets, values).slope
            stats.append(Stats(window, len(values), mean(values) if values else None, sum(values), met, trend))
        return Analysis(metric, tuple(stats), target)

    def bedtime_summary(self, window: Window) -> str:
        minutes = self.store.bedtime_minutes(window)
        if not minutes:
            return ""
        # Put the day boundary at noon before summarizing times clustered near midnight.
        typical = (round(median((minute - 720) % 1440 for minute in minutes)) + 720) % 1440
        return f"{window}有 {len(minutes)}/{window.days} 晚有效入睡时间，典型入睡时刻约为 {typical // 60:02d}:{typical % 60:02d}。"


@dataclass(frozen=True)
class Interpretation:
    topic: str
    text: str
    used_model: bool
    evidence: EvidenceReport | None = None


_METHOD_NOTES = {
    "average": "日均值按实际有记录的天数计算。数值为 0 也计入记录。",
    "coverage": "缺失日期不计入平均，也不计为未达标。记录天数太少时，均值只能描述已记录的日期。",
    "comparison": "比较使用两个窗口各自的日均值。记录覆盖或构成不同时，变化可能受采样影响。",
}


class DomainExpertAgent:
    """Interpret measurements using the project's calculation and evidence notes."""

    def __init__(self, model: Ollama | None = None, evidence: EvidenceResearch | None = None):
        self.model = model
        self.evidence = evidence

    def run(self, metric: Metric, analysis: Analysis | None, subquery: str, *,
            skill: ActiveSkill | None = None) -> Interpretation:
        choices = ["average"]
        if analysis and any(item.samples < item.window.days for item in analysis.stats):
            choices.append("coverage")
        if analysis and len(analysis.stats) > 1:
            choices.append("comparison")
        choice = None
        if self.model and len(choices) > 1:
            choice = self.model.choose(
                skill_prompt("你负责解释统计结果。根据测量覆盖和比较窗口，选择最应该提醒用户的统计方法主题。", skill),
                {"task": subquery, "metric": metric.label,
                 "sample_counts": [(item.samples, item.window.days) for item in analysis.stats]},
                tuple(choices),
            )
        selected = choice or ("coverage" if "coverage" in choices else choices[-1])
        text = _METHOD_NOTES[selected]
        if "coverage" in choices and selected != "coverage":
            text = _METHOD_NOTES["coverage"] + text
        return Interpretation(selected, text, choice is not None)

    def research(self, scope: EvidenceScope, personal_observation: str = "", *,
                 skill: ActiveSkill | None = None, personal_analysis: VerifiedComparison | None = None) -> Interpretation:
        if self.evidence is None:
            raise ValueError("外部证据检索未启用。")
        report = self.evidence.run(scope, personal_observation, skill=skill, personal_analysis=personal_analysis)
        return Interpretation("external_evidence", report.describe(), report.used_model, report)


@dataclass(frozen=True)
class Coaching:
    text: str
    used_model: bool = False


_ACTIONS = {
    Metric.SLEEP: {
        "record": "接下来一周每天记录睡眠时长，在周末一起查看有记录的天数和日均值。",
        "routine": "挑一个适合你的晚间时间，连续一周在这个时间做睡前准备，并记录第二天的睡眠时长。",
    },
    Metric.STEPS: {
        "record": "接下来一周每天记录步数，在周末一起查看日均值和有记录的天数。",
        "routine": "挑一个你愿意尝试的步行时段，一周后结合步数记录评估这个安排是否适合你。",
    },
}


class HealthCoachAgent:
    def __init__(self, model: Ollama | None = None):
        self.model = model

    def plan(self, metric: Metric, analysis: Analysis | None, interpretation: Interpretation | None,
             goal: float | None, subquery: str, *, skill: ActiveSkill | None = None) -> Coaching:
        evidence = interpretation.evidence if interpretation else None
        if evidence and evidence.question_scope == "步数与当晚睡眠":
            if not evidence.personal_observation:
                return Coaching("先连续记录一周的每日步数与入睡日期，再检查哪些日期能配对。")
            text = "接下来一周继续记录每日步数和当晚睡眠，周末按同样口径对比两组。"
            if not any(finding.fit == "direct" for finding in evidence.findings):
                text += "先把差异当作待验证的观察，记录是否在多周重复出现。"
            return Coaching(text)
        actions = _ACTIONS[metric]
        samples = sum(item.samples for item in analysis.stats) if analysis else None
        incomplete = bool(analysis and any(item.samples < item.window.days for item in analysis.stats))
        choice = None
        if self.model and (samples or analysis is None):
            context = {"task": subquery, "metric": metric.label, "samples": samples,
                       "analysis_note": interpretation.text if interpretation else None, "current_goal": goal}
            if evidence:
                context["evidence"] = evidence.coach_context()
            choice = self.model.choose(
                skill_prompt("你是日常习惯教练。根据记录和统计解释，为用户挑一个下一步。record 表示优先补记，routine 表示尝试固定时段。", skill),
                context,
                tuple(actions),
            )
        action = choice or ("record" if incomplete else "routine")
        goal_text = (
            f"当前登记的目标为每天 {goal:g} {metric.unit}。" if goal is not None
            else f"如果你愿意跟踪达标情况，可以告诉我你想设定的每日{metric.label}目标。"
        )
        return Coaching(f"建议的下一步：{actions[action]}{goal_text}", choice is not None)

    def review(self, analysis: Analysis, interpretation: Interpretation | None) -> Coaching:
        metric = analysis.metric
        if analysis.target is None:
            return Coaching(f"还没有保存{metric.label}目标。你可以明确给出想设定的每日数值。")
        goal = f"按当前目标每天 {analysis.target:g} {metric.unit}回看"
        rows = [f"{item.window} 有 {item.samples} 天记录，其中 {item.met} 天达标" for item in analysis.stats if item.samples]
        if not rows:
            note = interpretation.text if interpretation else ""
            return Coaching(f"{goal}，所选时间没有足够记录用于复盘。{note}")
        return Coaching(f"{goal}，{'；'.join(rows)}。下一轮可以继续按同一目标复盘。")
