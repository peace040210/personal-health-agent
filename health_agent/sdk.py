"""Structured, local tools shared by the CLI, model dispatcher, and MCP."""

import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from .agents import Analysis, DataScienceAgent, HealthCoachAgent
from .domain import Metric, Window
from .ds_analysis import SandboxAnalysis, SandboxDataScience
from .model import WorkflowModel
from .skills import ActiveSkill
from .store import Store


def _object(**properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def _choice(*values: str) -> dict:
    return {"type": "string", "enum": list(values)}


_DATE = {"type": "string", "format": "date", "description": "Calendar date in YYYY-MM-DD, inclusive."}
_METRIC = _choice(*(metric.value for metric in Metric))
_WINDOW = _object(start=_DATE, end=_DATE)
_METRIC_WINDOWS = _object(metric=_METRIC, windows={"type": "array", "items": _WINDOW, "minItems": 1})
_SLEEP = _choice("sleep_hours", "deep_sleep_hours")
_ALIGNMENT = _choice("bedtime_day", "day")
_THRESHOLD = _choice("median", "goal")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict
    read_only: bool = True

    def function(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": deepcopy(self.parameters)}}


_SPECS = (
    ToolSpec("query_metric", "Summarize steps or sleep and compare the given date windows.", _METRIC_WINDOWS),
    ToolSpec("review_goal", "Count recorded days that met the current saved goal in each window.", _METRIC_WINDOWS),
    ToolSpec("analyze_steps_sleep", "Compare mean sleep duration across step-count groups in one date window.",
             _object(start=_DATE, end=_DATE, sleep_metric=_SLEEP, alignment=_ALIGNMENT, threshold=_THRESHOLD)),
    ToolSpec("get_goal", "Read the current saved daily goal for a metric.", _object(metric=_METRIC)),
    ToolSpec("import_daily_records", "Validate and import daily records from a local CSV file.",
             _object(path={"type": "string", "minLength": 1}), read_only=False),
)
_REGISTRY = {spec.name: spec for spec in _SPECS}


def _fields(value: object, names: list[str]) -> None:
    if not isinstance(value, dict) or set(value) != set(names):
        raise ValueError(f"Expected exactly these fields: {', '.join(names)}.")


def _enum(value: object, schema: dict) -> str:
    if not isinstance(value, str) or value not in schema["enum"]:
        raise ValueError(f"Expected one of: {', '.join(schema['enum'])}.")
    return value


def _date(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError("Dates must be in YYYY-MM-DD format.")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Dates must be in YYYY-MM-DD format.")
    return parsed


def _window(start: object, end: object) -> Window:
    window = Window(_date(start), _date(end))
    if window.end < window.start:
        raise ValueError("The end date must be on or after the start date.")
    return window


def _windows(values: object) -> tuple[Window, ...]:
    if not isinstance(values, list) or not values:
        raise ValueError("Provide at least one date window.")
    windows = []
    for value in values:
        _fields(value, ["start", "end"])
        windows.append(_window(value["start"], value["end"]))
    return tuple(windows)


def metric_arguments(metric: Metric, windows: tuple[Window, ...]) -> dict:
    return {"metric": metric.value, "windows": [{"start": window.start.isoformat(), "end": window.end.isoformat()}
                                               for window in windows]}


class HealthTools:
    """Call registered tools with JSON-compatible arguments and results."""

    def __init__(self, store: Store, *, ds_model: WorkflowModel | None = None):
        self.store = store
        self.ds = DataScienceAgent(store)
        self.sandbox = SandboxDataScience(store, ds_model) if ds_model is not None else None

    def specs(self, *, read_only: bool = False) -> tuple[ToolSpec, ...]:
        return tuple(deepcopy(spec) for spec in _SPECS
                     if not read_only or (spec.read_only and (spec.name != "analyze_steps_sleep" or self.sandbox)))

    def function(self, name: str) -> dict:
        spec = _REGISTRY.get(name)
        if spec is None or not spec.read_only:
            raise ValueError(f"Tool is unavailable to the model: {name}")
        return spec.function()

    def call(self, name: str, arguments: dict) -> dict:
        result = self.execute(name, arguments)
        if isinstance(result, dict):
            return result
        if isinstance(result, SandboxAnalysis):
            return {"status": result.status, "reply": result.reply, "plan": asdict(result.plan) if result.plan else None,
                    "attempts": [attempt.status for attempt in result.attempts], "result": result.result}
        payload = json.loads(json.dumps(asdict(result), default=date.isoformat))
        if name == "review_goal":
            payload["reply"] = HealthCoachAgent().review(result, None).text
        return payload

    def execute(self, name: str, arguments: dict, *, goals: dict[Metric, float] | None = None,
                question: str = "", subquery: str = "", skill: ActiveSkill | None = None) -> Analysis | SandboxAnalysis | dict:
        spec = _REGISTRY.get(name)
        if spec is None:
            raise ValueError(f"Unknown health tool: {name}")
        _fields(arguments, spec.parameters["required"])

        if name == "import_daily_records":
            path = arguments["path"]
            if not isinstance(path, str) or not path.strip():
                raise ValueError("Provide a non-empty local CSV path.")
            return {"days": self.store.import_csv(Path(path))}

        if name == "analyze_steps_sleep":
            window = _window(arguments["start"], arguments["end"])
            sleep_metric = _enum(arguments["sleep_metric"], _SLEEP)
            alignment = _enum(arguments["alignment"], _ALIGNMENT)
            threshold = _enum(arguments["threshold"], _THRESHOLD)
            if self.sandbox is None:
                return SandboxAnalysis("unavailable", "步数与睡眠的配对分析需要启用 DS 沙箱。")
            step_goal = self._goal(Metric.STEPS, goals) if threshold == "goal" else None
            return self.sandbox.compare(window, alignment=alignment, sleep_metric=sleep_metric,
                                        threshold=threshold, step_goal=step_goal,
                                        question=question or "按指定口径比较步数与当晚睡眠。",
                                        subquery=subquery or "计算两组的有效天数和平均睡眠小时数。", skill=skill)

        metric = Metric(_enum(arguments["metric"], _METRIC))
        if name == "get_goal":
            return {"metric": metric.value, "target": self._goal(metric, goals), "unit": metric.unit}
        target = self._goal(metric, goals) if name == "review_goal" else None
        return self.ds.run(metric, _windows(arguments["windows"]), target)

    def _goal(self, metric: Metric, current: dict[Metric, float] | None) -> float | None:
        return (self.store.goals() if current is None else current).get(metric)
