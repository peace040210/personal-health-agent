import re
from dataclasses import asdict, dataclass
from math import isclose, isfinite
from statistics import mean, median

from .domain import Task, Window
from .ds_sandbox import PythonSandbox
from .model import WorkflowModel
from .skills import ActiveSkill, skill_prompt
from .store import Store


_PLAN_PROMPT = """你是 DS 分析规划器。使用已确认的 scope 和提供的表结构，生成步数分组与睡眠时长的分析计划。
逐项照录 scope。approach 用中文说明日期对齐、配对后确定分组阈值、缺失处理、两组样本数和平均睡眠时长的差值。根据可用记录设置每组至少 2 到 10 个有效日。不要添加其他统计量。
当 threshold 是 median 时，使用有效配对日的步数中位数，高步数组要求步数严格大于中位数。
当 threshold 是 goal 时，高步数组要求步数大于或等于用户登记的步数目标。不要生成代码。只输出 JSON。"""
_CODE_PROMPT = """Generate one Python function `def analysis(rows, plan):` implementing the supplied analysis plan.
Reply with a JSON object with the single key "code". All input keys exist even when their values are None.
Implement date alignment using dictionary comprehensions like these:
    start, end = plan["start"], plan["end"]
    alignment, metric = plan["alignment"], plan["sleep_metric"]
    steps_by_day = {r["day"]: r["steps"] for r in rows if start <= r["day"] <= end and r["steps"] is not None}
    sleep_by_day = {r[alignment]: r[metric] for r in rows if r[alignment] is not None and r[metric] is not None}
    days = [d for d in steps_by_day if d in sleep_by_day]
The sleep recording date can be outside the range; the step date is the one filtered by the window.
For a median threshold, create a list of paired step counts, call median on it, and use a strict > comparison.
For a goal threshold, use plan["step_goal"] and >=. Iterate over days directly to collect each group's dates.
Collect each group's hours in a separate list, then call mean on each list. Both groups are guaranteed nonempty.
Return exactly paired_days, step_cutoff, high_days, other_days, high_mean_hours, other_mean_hours, difference_hours.
The fields ending in _days are counts. Subtract the other group's mean from the high group's mean, without rounding.
Allowed: assignments, if, lookups with brackets, len, mean, median, and flat comprehensions over a named variable.
Forbidden: imports, attribute access (including .get and .update), for/while statements, range and enumerate.
Use the previous attempt's feedback, when supplied, to return a complete corrected function."""
_REVIEW_PROMPT = """检查 DS 的分析计划、执行代码和统计结果。判断代码有无用错字段、日期归属或分组规则。
结果符合计划时返回 decision 为 summary、feedback 为空串。发现具体逻辑错误时返回 decision 为 code_update，feedback 写出待修正的位置和理由。只输出 JSON。"""
_COLUMNS = {
    "day": "ISO 格式的记录日期", "steps": "每日步数，整数或 null",
    "bedtime_day": "ISO 格式的入睡日期或 null", "sleep_hours": "总睡眠小时数或 null",
    "deep_sleep_hours": "深睡小时数或 null",
}


def _schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


_CODE_SCHEMA = _schema({"code": {"type": "string"}})
_REVIEW_SCHEMA = _schema({"decision": {"enum": ["summary", "code_update"]}, "feedback": {"type": "string"}})


def comparison_arguments(task: Task) -> dict:
    question = task.analysis_question or ""
    if not question or len(task.windows) != 1:
        raise ValueError("请为步数与睡眠的配对分析指定一个连续的时间窗口。")
    if any(term in question for term in ("相关系数", "显著性", "因果", "导致", "睡眠评分", "睡眠效率", "入睡时间", "几点睡")):
        raise ValueError("当前联合分析按步数分组比较总睡眠或深睡时长。")
    if re.search(r"\d+(?:\.\d+)?\s*(?:万\s*)?步", question):
        raise ValueError("当前分组支持个人中位数或已登记的步数目标。请用其中一种口径。")
    window, = task.windows
    by_goal = any(term in question for term in ("步数目标", "步数达标", "走路达标"))
    return {"start": window.start.isoformat(), "end": window.end.isoformat(),
            "alignment": "day" if "记录日" in question else "bedtime_day",
            "sleep_metric": "deep_sleep_hours" if "深睡" in question or "深度睡眠" in question else "sleep_hours",
            "threshold": "goal" if by_goal else "median"}


@dataclass(frozen=True)
class AnalysisPlan:
    start: str
    end: str
    alignment: str
    sleep_metric: str
    threshold: str
    step_goal: float | None
    minimum_per_group: int
    approach: str


@dataclass(frozen=True)
class Attempt:
    number: int
    status: str
    code: str | None = None
    feedback: str = ""


@dataclass(frozen=True)
class VerifiedComparison:
    plan: AnalysisPlan
    statistics: dict[str, int | float]


@dataclass(frozen=True)
class SandboxAnalysis:
    status: str
    reply: str
    plan: AnalysisPlan | None = None
    attempts: tuple[Attempt, ...] = ()
    result: dict | None = None

    @property
    def verified_comparison(self) -> VerifiedComparison | None:
        if self.status == "ok" and self.plan and self.result is not None:
            return VerifiedComparison(self.plan, self.result.copy())
        return None


def _pairs(rows: list[dict], scope: dict) -> list[tuple[int, float]] | None:
    start, end = scope["start"], scope["end"]
    steps = {row["day"]: row["steps"] for row in rows
             if row["steps"] is not None and start <= row["day"] <= end}
    sleep = {}
    for row in rows:
        day = row[scope["alignment"]]
        value = row[scope["sleep_metric"]]
        if day in steps and value is not None:
            if day in sleep:
                return None
            sleep[day] = value
    return [(steps[day], value) for day, value in sleep.items()]


def _reference(pairs: list[tuple[int, float]], plan: AnalysisPlan) -> dict | None:
    cutoff = plan.step_goal if plan.threshold == "goal" else median(steps for steps, _ in pairs)
    high, other = [], []
    for steps, hours in pairs:
        matched = steps >= cutoff if plan.threshold == "goal" else steps > cutoff
        (high if matched else other).append(hours)
    if min(len(high), len(other)) < plan.minimum_per_group:
        return None
    return {"paired_days": len(pairs), "step_cutoff": cutoff, "high_days": len(high), "other_days": len(other),
            "high_mean_hours": mean(high), "other_mean_hours": mean(other), "difference_hours": mean(high) - mean(other)}


def _discrepancies(value: dict, expected: dict) -> str:
    if value.keys() != expected.keys():
        return "The result fields do not match the required output keys."
    wrong = []
    for key, reference in expected.items():
        actual = value[key]
        if type(actual) not in {int, float} or not isinstance(reference, (int, float)):
            wrong.append(key)
        elif key.endswith("days"):
            if type(actual) is not int or actual != reference:
                wrong.append(key)
        elif abs(actual) > 1e20 or not isfinite(actual) or not isclose(actual, reference, rel_tol=1e-8, abs_tol=1e-9):
            wrong.append(key)
    if not wrong:
        return ""
    return f"Independent verification disagrees on {', '.join(wrong)}. Check date alignment, threshold and group membership."


def _describe(plan: AnalysisPlan, result: dict) -> str:
    metric = "深睡时长" if plan.sleep_metric == "deep_sleep_hours" else "总睡眠时长"
    alignment = "以入睡日与步数日期配对" if plan.alignment == "bedtime_day" else "按同一记录日配对"
    cutoff = result["step_cutoff"]
    grouping = f"步数达到目标 {cutoff:g} 步" if plan.threshold == "goal" else f"步数高于配对日中位数 {cutoff:g} 步"
    return (f"{plan.start} 至 {plan.end}，{alignment}，共 {result['paired_days']} 个有效配对日。"
            f"{grouping}的 {result['high_days']} 天，平均{metric} {result['high_mean_hours']:.2f} 小时。"
            f"其余 {result['other_days']} 天的平均值为 {result['other_mean_hours']:.2f} 小时。"
            f"两组平均值之差（高步数组减其余组）为 {result['difference_hours']:+.2f} 小时。")


class SandboxDataScience:
    def __init__(self, store: Store, model: WorkflowModel):
        self.store = store
        self.model = model
        self.sandbox = PythonSandbox()

    def _plan(self, scope: dict, question: str, subquery: str, pair_count: int,
              skill: ActiveSkill | None) -> AnalysisPlan | None:
        schema = _schema({**{key: {"enum": [value]} for key, value in scope.items()},
                          "minimum_per_group": {"type": "integer", "minimum": 2, "maximum": 10},
                          "approach": {"type": "string", "minLength": 1, "maxLength": 500}})
        plan = self.model.structured(skill_prompt(_PLAN_PROMPT, skill), {"question": question, "subquery": subquery, "scope": scope,
                                                   "columns": _COLUMNS, "paired_days": pair_count}, schema)
        if not plan or plan.keys() != schema["properties"].keys() or any(plan[key] != value for key, value in scope.items()):
            return None
        minimum, approach = plan["minimum_per_group"], plan["approach"]
        if type(minimum) is not int or not 2 <= minimum <= 10 or not isinstance(approach, str) or not 1 <= len(approach.strip()) <= 500:
            return None
        return AnalysisPlan(**scope, minimum_per_group=minimum, approach=approach.strip())

    def run(self, task: Task, subquery: str, step_goal: float | None, *,
            skill: ActiveSkill | None = None) -> SandboxAnalysis:
        if not self.sandbox.available:
            return SandboxAnalysis("unavailable", "这台机器没有可用的 DS Python 沙箱。")
        try:
            arguments = comparison_arguments(task)
        except ValueError as error:
            return SandboxAnalysis("unsupported", str(error))
        return self.compare(task.windows[0], alignment=arguments["alignment"],
                            sleep_metric=arguments["sleep_metric"], threshold=arguments["threshold"],
                            step_goal=step_goal, question=task.analysis_question or "", subquery=subquery, skill=skill)

    def compare(self, window: Window, *, alignment: str, sleep_metric: str, threshold: str,
                step_goal: float | None, question: str, subquery: str,
                skill: ActiveSkill | None = None) -> SandboxAnalysis:
        if not self.sandbox.available:
            return SandboxAnalysis("unavailable", "这台机器没有可用的 DS Python 沙箱。")
        if threshold == "goal" and step_goal is None:
            return SandboxAnalysis("insufficient_data", "按步数达标分组需要先有已登记的每日步数目标。")
        scope = {"start": window.start.isoformat(), "end": window.end.isoformat(),
                 "alignment": alignment, "sleep_metric": sleep_metric, "threshold": threshold,
                 "step_goal": step_goal if threshold == "goal" else None}
        rows = self.store.analysis_rows(window)
        pairs = _pairs(rows, scope)
        if pairs is None:
            return SandboxAnalysis("insufficient_data", "同一天有多条可配对的睡眠记录，需要先确认该使用哪条。")
        if len(pairs) < 4:
            return SandboxAnalysis("insufficient_data", f"{window} 仅有 {len(pairs)} 个有效的步数与睡眠配对日。"
                                   "每组至少需要两个有效日。当晚分析还需要入睡日期。")

        plan = self._plan(scope, question, subquery, len(pairs), skill)
        if plan is None:
            return SandboxAnalysis("planning_error", "DS 没能生成符合已确认数据范围的分析计划。")
        expected = _reference(pairs, plan)
        if expected is None:
            return SandboxAnalysis("insufficient_data", f"有 {len(pairs)} 个有效配对日，分组后未达到每组至少 {plan.minimum_per_group} 天的要求。", plan)

        request = {"question": question, "subquery": subquery, "columns": _COLUMNS, "plan": asdict(plan)}
        attempts = []
        best_result = None
        review_used = False
        for number in range(1, 6):
            if attempts:
                request["previous_attempt"] = asdict(attempts[-1])
            prompt = skill_prompt(_CODE_PROMPT, skill, reference="code-repair.md" if attempts else None)
            generated = self.model.structured(prompt, request, _CODE_SCHEMA, max_tokens=1400)
            code = generated.get("code") if generated and set(generated) == {"code"} else None
            if not isinstance(code, str) or not code.strip():
                attempts.append(Attempt(number, "generation_error", feedback="Return JSON with one code string."))
                if len(attempts) > 1 and attempts[-2].status == "generation_error":
                    break
                continue
            execution = self.sandbox.run(code, rows, asdict(plan))
            if execution.status != "ok":
                attempts.append(Attempt(number, execution.status, code, execution.feedback))
                if execution.status == "unavailable":
                    break
                continue
            result = execution.output
            feedback = _discrepancies(result, expected)
            if feedback:
                attempts.append(Attempt(number, "result_error", code, feedback))
                continue

            best_result = result
            if not review_used and number < 5:
                review_used = True
                review = self.model.structured(_REVIEW_PROMPT, {**request, "code": code, "result": result}, _REVIEW_SCHEMA)
                if review and review.get("decision") == "code_update" and isinstance(review.get("feedback"), str) and review["feedback"].strip():
                    attempts.append(Attempt(number, "code_update", code, review["feedback"].strip()[:500]))
                    continue
            attempts.append(Attempt(number, "verified", code))
            break

        if best_result is not None:
            return SandboxAnalysis("ok", _describe(plan, best_result), plan, tuple(attempts), best_result)
        note = "已尝试生成和修正代码，未得到通过校验的结果。" if attempts[-1].status != "unavailable" else attempts[-1].feedback
        return SandboxAnalysis("failed", f"DS 沙箱分析未完成。{note}", plan, tuple(attempts))
