"""Per-session coaching moves and the information needed to advance them."""

import re
from dataclasses import dataclass, replace

from .domain import CoachMemory, Context, Intent, Metric, Route, Status, Task
from .model import WorkflowModel
from .skills import ActiveSkill, skill_prompt


_CLOSE = r"(?:就这样|就到这(?:里)?|先到这(?:里)?|先这样|结束(?:对话)?|够了)"
_END = re.compile(rf"^(?:谢谢(?:你)?(?:[，, ]*{_CLOSE})?|{_CLOSE})[。！! ]*$")
_ACCEPT = re.compile(r"^(?:好(?:的)?|可以|同意|就按这个来|就这么做|我愿意试试|好[，,]我先试)[。！! ]*$")
_BARRIER = re.compile(r"太难|没时间|没空|加班|太忙|坚持不了|做不到|不方便|很难|不现实|太累|太晚|时间少")
_TIME_BUDGET = re.compile(r"(?:只有|最多|能抽出|有)\s*[\d一二两三四五六七八九十]+\s*分钟|[\d一二两三四五六七八九十]+\s*分钟(?:内|以内|左右)")
_CONTEXT = re.compile(r"通勤|下班|上班|饭后|午休|晚上|早上|周末|睡前|白天|白天更有精神|提前一站")
_SLEEP_AIM = re.compile(r"更有精神|睡得更久|睡久一点|早(?:点|些)睡|早点入睡|入睡更|入睡时间|规律|作息")
_BEDTIME_AIM = re.compile(r"早(?:点|些)睡|早点入睡|入睡更|入睡时间|规律|作息")
_MOTIVATION = re.compile(r"(?:因为|为了|原因是)([^。！？；，,]{2,100})")
_CLOCK = re.compile(r"\b(?:[01]?\d|2[0-3])[:：][0-5]\d\b|(?:[零一二两三四五六七八九十\d]+)点(?:半|[零一二两三四五六七八九十\d]+分)?")
_NEW_PLAN = re.compile(r"想|希望|打算|计划|建议|改善|怎么")
_ACTION = re.compile(r"可以|愿意|打算|准备|我会|我先|试试|我能")
_CONCRETE = re.compile(r"提前一站|下车|散步|步行|走\s*\d|走\s*[一两三五十几]分钟|关掉|放下|准备睡觉|睡前.+分钟|呼吸放松")
_PROGRESS = re.compile(r"做了|试过|试了|走了|完成了|坚持了|没做到|没做成")
_NEGATIVE = re.compile(r"不用|不想|不要|别|不能|不愿意|不打算|没法")
_PLAN_REFERENCE = re.compile(r"(?:上周|上次|之前|那个|这个|原来的|刚才).{0,10}计划|想调整|调整一下|再简单点")
_PLAN_REVIEW = re.compile(r"(?:复盘|回顾|总结).{0,18}(?:计划|安排|行动)|(?:计划|安排|行动).{0,18}(?:复盘|回顾)")


def _metric(text: str) -> Metric | None:
    sleep = bool(re.search(r"睡眠|睡觉|睡得|入睡|早点睡|作息|白天更有精神", text))
    steps = bool(re.search(r"步数|走路|步行|多走|少走|散步|提前一站", text))
    if sleep == steps:
        return None
    return Metric.SLEEP if sleep else Metric.STEPS


def coaching_route(route: Route, message: str, context: Context) -> Route:
    if route.unsupported_journeys or (route.clarification or "").startswith(("目前可以", "症状评估")):
        return route
    if any(task.status is Status.DEFERRED or task.status is Status.NEGATED and task.intent is Intent.PLAN for task in route.tasks):
        return route
    text = message.strip()
    metric = _metric(text)
    latest = next((memory for memory in context.coaching if memory.metric == metric), None) if metric else None
    latest = latest or (context.coaching[0] if context.coaching and not metric else None)
    active = [task for task in route.tasks if task.status is Status.ACTIVE]
    focus_on_saved_plan = latest and (
        _PLAN_REVIEW.search(text) or _PLAN_REFERENCE.search(text) and _BARRIER.search(text)
    )
    if active and not (focus_on_saved_plan and all(task.intent in {Intent.PLAN, Intent.REVIEW} for task in active)
                       and not re.search(r"(?:步数|睡眠)目标|达标|(?:查|根据|结合).{0,12}记录", text)):
        return route
    continuation = latest and (
        _END.fullmatch(text) or _PLAN_REFERENCE.search(text) or _PLAN_REVIEW.search(text)
        or _BARRIER.search(text) or _TIME_BUDGET.search(text) or _PROGRESS.search(text)
        or (_ACCEPT.fullmatch(text) and latest.pending == "confirm")
        or (latest.pending and any(pattern.search(text) for pattern in (_ACTION, _CONTEXT, _SLEEP_AIM, _CLOCK, _MOTIVATION)))
    )
    if continuation:
        metric = latest.metric
    else:
        opening = re.split(r"[。？！？，,；;]", text, maxsplit=1)[0]
        if not (metric and _NEW_PLAN.search(opening) and not _NEGATIVE.search(opening)):
            return route
    task = Task(Intent.PLAN, Status.ACTIVE, len(route.tasks), (metric,))
    inactive = tuple(item for item in route.tasks if item.status is not Status.ACTIVE)
    return replace(route, tasks=(*inactive, task), candidates=tuple(dict.fromkeys((*route.candidates, Intent.PLAN))),
                   clarification=None, pending_message=None)


@dataclass(frozen=True)
class CoachDraft:
    action: str
    reply: str
    memory: CoachMemory
    missing_fact: str = ""
    used_model: bool = False


class CoachingLoop:
    def __init__(self, model: WorkflowModel | None = None):
        self.model = model

    def draft(self, message: str, metric: Metric, memory: CoachMemory | None,
              observation: str = "", *, skill: ActiveSkill | None = None,
              domain_context: dict | str | None = None) -> CoachDraft:
        state = memory or CoachMemory(metric)
        text = message.strip()
        user_words = text[:180]
        if _END.fullmatch(text):
            note = f"本次确定的行动是「{state.agreed_action}」。你下次可以回来复盘。" if state.agreed_action else "今天先到这里。下次可以接着讨论适合你的做法。"
            if state.objective:
                note = f"这次聊的目标是「{state.objective}」。" + note
            return CoachDraft("close", note, replace(state, closed=True, pending=""))
        if state.pending == "confirm" and state.proposal and _ACCEPT.fullmatch(text):
            return CoachDraft("agree", f"记下了。你愿意先试「{state.proposal}」。之后可以回来看看这个安排是否适合你。",
                              replace(state, agreed_action=state.proposal, proposal="", pending="", closed=False))

        objective = state.objective
        if metric is Metric.SLEEP:
            aim = _SLEEP_AIM.search(text)
            if aim:
                objective = aim[0]
        elif not objective:
            objective = "增加日常步行"
        state = replace(state, objective=objective, closed=False, last_observation=observation or state.last_observation)
        motivation = _MOTIVATION.search(text)
        if motivation:
            state = replace(state, motivation=motivation[1].strip())
        if _BARRIER.search(text) or _TIME_BUDGET.search(text):
            state = replace(state, barrier=user_words)
        if _CONTEXT.search(text):
            state = replace(state, preference=user_words)
        if _PROGRESS.search(text):
            state = replace(state, progress=user_words)

        has_action = bool(_ACTION.search(text) and _CONCRETE.search(text) and not _NEGATIVE.search(text) and not _BARRIER.search(text))
        if has_action:
            return self._propose(state, user_words)
        if (state.pending == "confirm" and state.proposal and not _BARRIER.search(text)
                and not _TIME_BUDGET.search(text) and not _PROGRESS.search(text)):
            return self._propose(state, state.proposal)
        if state.agreed_action and _BARRIER.search(text):
            reduced = ("挑一个最方便的日子，先步行一小段" if metric is Metric.STEPS
                       else "只挑一个晚上，先留出几分钟做睡前准备")
            return self._propose(state, reduced, prefix=f"上次定的是「{state.agreed_action}」。听起来这个难度偏高。")
        if state.agreed_action and (_PROGRESS.search(text) or _PLAN_REVIEW.search(text)):
            previous = f"上次的行动是「{state.agreed_action}」。"
            if state.progress:
                previous += f"你反馈「{state.progress.rstrip('。！？')}」。你想保留原安排，还是把它调轻一点？"
            else:
                previous += "这几天有哪一次做到了？"
            return CoachDraft("review", previous, replace(state, pending="preference"))
        if metric is Metric.SLEEP and not state.objective:
            return CoachDraft("clarify", "你更想睡得久一点、让入睡时间更规律，还是白天更有精神？",
                              replace(state, pending="objective"))

        if metric is Metric.SLEEP and _BEDTIME_AIM.search(state.objective) and not state.barrier and not state.preference:
            if _CLOCK.search(text):
                return self._after_bedtime(replace(state, last_observation=user_words))
            return CoachDraft("clarify", "你平时大概几点入睡？", replace(state, pending="bedtime"), "bedtime")
        if not state.preference and not state.barrier:
            reply = ("在哪个时段加一点步行最容易坚持，比如通勤、午休或饭后？" if metric is Metric.STEPS
                     else "最近有什么安排最影响你实现这个睡眠目标？")
            return CoachDraft("clarify", reply, replace(state, pending="preference"))

        if metric is Metric.STEPS:
            timing = next((word for word in ("通勤", "午休", "饭后", "周末") if word in state.preference), "")
            choices = ((f"选一天，在{timing}时步行一小段" if timing else "挑一个你方便的时段步行一小段"),
                       "先选一天，在已有行程里多走一小段")
        else:
            choices = ("挑一个晚上，提前几分钟做睡前准备", "选一件容易坚持的睡前准备动作，先试一晚")
        choice = self._select_action(state, choices, skill, domain_context)
        return replace(self._propose(state, choices[choice or 0]), used_model=choice is not None)

    def revise_bedtime(self, draft: CoachDraft, observation: str) -> CoachDraft:
        if not observation:
            return draft
        return self._after_bedtime(replace(draft.memory, last_observation=observation), observation)

    def _after_bedtime(self, state: CoachMemory, prefix: str = "") -> CoachDraft:
        return CoachDraft("clarify", prefix + "什么最容易打乱你想要的入睡时间？", replace(state, pending="preference"))

    def _propose(self, state: CoachMemory, proposal: str, prefix: str = "") -> CoachDraft:
        proposed = proposal.rstrip("。！？ ")
        return CoachDraft("propose", f"{prefix}先试一个小行动「{proposed}」。你愿意按这个安排试一次吗？",
                          replace(state, proposal=proposed, pending="confirm"))

    def _select_action(self, state: CoachMemory, choices: tuple[str, ...], skill: ActiveSkill | None,
                       domain_context: dict | str | None) -> int | None:
        if self.model is None:
            return None
        schema = {"type": "object", "properties": {"choice": {"enum": list(range(len(choices)))}},
                  "required": ["choice"], "additionalProperties": False}
        payload = {"objective": state.objective, "motivation": state.motivation,
                   "barrier": state.barrier, "preference": state.preference, "actions": list(choices)}
        if domain_context:
            payload["external_evidence_context"] = domain_context
        if skill and state.last_observation:
            payload["personal_observation"] = state.last_observation
        reply = self.model.structured(
            skill_prompt("你是日常健康教练。根据用户目标、限制和偏好，从候选中选出一项最容易坚持的行动。只返回 choice。",
                         skill, reference="adjustment.md" if state.barrier else None),
            payload, schema,
        )
        choice = reply.get("choice") if reply else None
        return choice if type(choice) is int and 0 <= choice < len(choices) else None
