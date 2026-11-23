"""Select bounded, read-only follow-up questions before publishing a draft."""

from dataclasses import asdict, dataclass

from .model import WorkflowModel


@dataclass(frozen=True)
class ReflectionQuestion:
    agent: str
    fact: str
    question: str


@dataclass(frozen=True)
class ReflectionReview:
    decision: str
    reflection_questions: tuple[ReflectionQuestion, ...]
    source: str


@dataclass(frozen=True)
class ReflectionRun:
    decision: str
    reflection_questions: tuple[ReflectionQuestion, ...]
    source: str
    findings: dict[str, str]
    revised: bool


_PROMPT = """你是健康多 Agent 系统的答复前审查器。你会收到主 Agent 草稿、已知发现和允许补查的问题。
草稿如果询问能够用已允许的辅助 Agent 补查的事实，返回 investigate 和对应的 reflection_questions。
用户的目标、动机、偏好和障碍应由用户确认。不要生成额外的 Agent、写入操作、查询指标或时间范围。
agent 和 fact 必须来自 allowed_reflection_questions。question 用一句简短的话解释查什么。只返回 JSON。"""


class GlobalReflector:
    def __init__(self, model: WorkflowModel | None = None):
        self.model = model

    def check(self, draft: str, action: str, known: dict,
              allowed: tuple[ReflectionQuestion, ...]) -> ReflectionReview:
        if not allowed:
            return ReflectionReview("clarify" if action == "clarify" else "accept", (), "rules")
        if self.model is None:
            return ReflectionReview("investigate", allowed, "rules")

        question_schema = {"type": "object", "properties": {
            "agent": {"enum": sorted({item.agent for item in allowed})},
            "fact": {"enum": sorted({item.fact for item in allowed})}, "question": {"type": "string"}},
            "required": ["agent", "fact", "question"], "additionalProperties": False}
        schema = {"type": "object", "properties": {
            "decision": {"enum": ["accept", "investigate", "clarify"]},
            "reflection_questions": {"type": "array", "items": question_schema, "maxItems": len(allowed)}},
            "required": ["decision", "reflection_questions"], "additionalProperties": False}
        result = self.model.structured(_PROMPT, {
            "draft": draft, "existing_findings": known,
            "allowed_reflection_questions": [asdict(item) for item in allowed],
        }, schema)
        if result and result.get("decision") == "investigate":
            selected = result.get("reflection_questions")
            keys = {(item.agent, item.fact) for item in allowed}
            if isinstance(selected, list) and len(selected) == len(allowed) and all(
                isinstance(item, dict) and set(item) == {"agent", "fact", "question"}
                and all(isinstance(value, str) for value in item.values())
                and 1 <= len(item["question"].strip()) <= 200 for item in selected
            ) and {(item["agent"], item["fact"]) for item in selected} == keys:
                return ReflectionReview("investigate", tuple(ReflectionQuestion(**item) for item in selected), "model")
        return ReflectionReview("investigate", allowed, "rules_fallback")
