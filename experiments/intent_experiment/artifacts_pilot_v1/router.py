import re
from datetime import date, timedelta
from functools import lru_cache
from hashlib import md5
from math import sqrt

from .domain import Context, Intent, Journey, Metric, Route, Status, Task, Window
from .model import IntentModel


INTENT_SYSTEM = "判断当前中文睡眠时长或步数请求的类型。只选择这一轮实际提出的动作。无法判断、否定、背景、明确写目标或超范围请选择 unknown。"
INTENT_CHOICES = ("analyze", "explain", "plan", "review_goal", "unknown")

_SENTENCES = re.compile(r"[。！？!?；;\n]+")
_SPLIT = re.compile(r"[，,]+|(?=顺便|另外|同时|但是|但(?=先|不要|不需要|只)|只(?:看|查|分析|比较)|再(?:看|查|比较|复盘|解释|制定))")
_METRICS = {
    Metric.SLEEP: re.compile(r"睡眠|睡觉|睡得|睡了|睡多久|睡多长|几点睡|睡眠时长"),
    Metric.STEPS: re.compile(r"步数|走路|走了|走多少|步行|\d\s*(?:万)?步"),
}
_SIGNALS = {
    Intent.SET_GOAL: re.compile(r"(?:设成|设为|改成|改为|定为|调整为|设置|设定)(?:我的)?(?:睡眠|步数)?(?:目标)?"),
    Intent.REVIEW: re.compile(r"复盘|达标|达成|进度|目标完成"),
    Intent.PLAN: re.compile(r"建议|推荐|计划|改善|怎么调整|制定|定个|目标"),
    Intent.EXPLAIN: re.compile(r"解释|解读|为什么|是什么意思|什么意思|怎么算|如何理解|什么是"),
    Intent.ANALYZE: re.compile(r"查|看|分析|比较|对比|趋势|走势|平均|总步数|怎么样|变化|差多少|多久|多少|数据|记录"),
}
_NEGATION = re.compile(r"不用|无需|不需要|不想|不要|不必|别|暂不|先不|不(?:看|查|比较|复盘|分析|解释|改|设置|制定)")
_CONDITION = re.compile(r"如果|假如|要是|除非|^(?:若|等到|等我)|(?:以后|稍后|之后|明天|下次)再")
_BACKGROUND = re.compile(r"之前|上次|刚才|已经")
_DATA_PREFACE = re.compile(r"^(?:根据|结合|基于|参考|用).*(?:记录|数据)$")
_CONTINUE = re.compile(r"^(?:那|就|看看?|查查?|选)?\s*(睡眠(?:时长)?|步数)[吧呢。！! ]*$")
_PROMPT = re.compile(r"问|想|请|帮我|看看|查查|复盘|怎么|呢|多少|为什么")
_SYMPTOMS = re.compile(r"症状|诊断|胸痛|胸闷|头晕|呼吸困难|吞咽困难|喉咙痛|昏厥")
_UNSUPPORTED = re.compile(r"用药|药量|药物|心率|血压|血糖|(?:入睡|起床)时间|几点(?:睡|起)")
_WINDOW = re.compile(r"最近(?:两|二|2)周|上上周|上周|本周|这周|这(?:两|二|2)周|(?:最近|过去|近)\s*(\d+|[一二两三四五六七八九十]+)\s*天|昨天|昨晚|今天")
_EXAMPLES = {
    Intent.ANALYZE: ("我上周平均睡了多久", "比较这周和上周的步数", "最近七天的睡眠趋势"),
    Intent.EXPLAIN: ("睡眠平均值怎么算", "为什么少几天记录会影响结论", "这个结果怎么理解"),
    Intent.PLAN: ("根据最近的睡眠记录帮我制定计划", "给一点改善步行规律的建议"),
    Intent.REVIEW: ("根据这周记录复盘我的睡眠目标", "上周有多少天步数达标"),
    Intent.SET_GOAL: ("把睡眠目标设为每天8小时", "请把步数目标改成每天8000步"),
}


@lru_cache(maxsize=256)
def _vector(text: str) -> tuple[float, ...]:
    """EchoMind 的本地字符 1–3 gram 哈希召回，面向新的例句库。"""
    text = re.sub(r"\s+", "", text.lower())
    counts = [0.0] * 256
    grams = {text[i:i + size] for size in (1, 2, 3) for i in range(len(text) - size + 1)}
    for gram in grams:
        digest = md5(gram.encode(), usedforsecurity=False).digest()
        index = int.from_bytes(digest[:4], "big") % len(counts)
        counts[index] += 1 if digest[4] % 2 == 0 else -1
    norm = sqrt(sum(value * value for value in counts)) or 1
    return tuple(value / norm for value in counts)


def _retrieve(text: str, limit: int = 3) -> list[tuple[Intent, str]]:
    query = _vector(text)
    scored = []
    for intent, examples in _EXAMPLES.items():
        score, example = max((sum(a * b for a, b in zip(query, _vector(example))), example) for example in examples)
        if score > 0:
            scored.append((score, intent, example))
    scored.sort(reverse=True)
    return [(intent, example) for _, intent, example in scored[:limit]]


def _metrics(text: str) -> tuple[Metric, ...]:
    return tuple(metric for metric, pattern in _METRICS.items() if pattern.search(text))


def _number(text: str) -> int:
    if text.isdigit():
        return int(text)
    digits = dict(zip("一二三四五六七八九", range(1, 10))) | {"两": 2}
    if "十" in text:
        left, _, right = text.partition("十")
        return 10 * digits.get(left, 1) + digits.get(right, 0)
    return digits.get(text, 0)


def _windows(text: str, today: date) -> tuple[Window, ...]:
    monday = today - timedelta(days=today.weekday())
    windows = []
    for match in _WINDOW.finditer(text):
        token = match[0]
        if match[1]:
            days = _number(match[1])
            if not 1 <= days <= 90:
                raise ValueError("请指定 1 到 90 天的时间范围。")
            windows.append(Window(today - timedelta(days=days - 1), today))
        elif token == "上上周":
            windows.append(Window(monday - timedelta(days=14), monday - timedelta(days=8)))
        elif token == "上周":
            windows.append(Window(monday - timedelta(days=7), monday - timedelta(days=1)))
        elif token in {"本周", "这周"}:
            windows.append(Window(monday, today))
        elif token.startswith("最近"):
            if re.search(r"比较|对比", text):
                windows.append(Window(today - timedelta(days=13), today - timedelta(days=7)))
                windows.append(Window(today - timedelta(days=6), today))
            else:
                windows.append(Window(today - timedelta(days=13), today))
        elif token.startswith("这"):
            windows.extend((Window(monday - timedelta(days=7), monday - timedelta(days=1)), Window(monday, today)))
        else:
            day = today if token == "今天" else today - timedelta(days=1)
            windows.append(Window(day, day))
    return tuple(sorted(set(windows), key=lambda window: (window.start, window.end)))


def _target(clause: str, metric: Metric) -> float | None:
    unit = r"(?:个)?小时" if metric is Metric.SLEEP else r"(万)?步"
    match = re.search(r"(?:设成|设为|改成|改为|定为|调整为|设置(?:为)?|设定(?:为)?)[^\d]{0,16}(\d+(?:\.\d+)?)\s*" + unit, clause)
    if not match:
        return None
    value = float(match[1])
    if metric is Metric.STEPS and match[2]:
        value *= 10_000
    return value if value > 0 and metric.valid(value) else None


class Router:
    def __init__(self, model: IntentModel | None = None):
        self.model = model

    def plan(self, message: str, context: Context, today: date) -> Route:
        message = re.sub(r"(?<=\d),(?=\d{3}\b)", "", message)
        selection = _CONTINUE.fullmatch(message)
        if selection and context.pending_message:
            message = f"{context.pending_message}，指标是{selection[1]}"
        clauses = [
            (sentence_id, clause.strip())
            for sentence_id, sentence in enumerate(_SENTENCES.split(message))
            for clause in _SPLIT.split(sentence) if clause.strip()
        ]
        mentioned = _metrics(message)
        last_metrics = mentioned or ((context.last_metric,) if context.last_metric else ())
        last_windows: tuple[Window, ...] = ()
        last_explicit_window = False
        tasks = []
        candidates = [intent for intent, _ in _retrieve(message)]
        conditional = False
        previous_sentence = None
        clarification = None
        pending = None
        unsupported_journeys = ()
        default_window = (Window(today - timedelta(days=6), today),)

        for index, (sentence_id, clause) in enumerate(clauses):
            if clause.startswith("指标是") and selection:
                continue
            if sentence_id != previous_sentence or clause.startswith(("但", "现在", "另外")):
                conditional = False
            previous_sentence = sentence_id
            conditional = conditional or bool(_CONDITION.search(clause))
            if _SYMPTOMS.search(clause) and not _NEGATION.search(clause):
                unsupported_journeys = (Journey.SYMPTOMS,)
                clarification = "症状评估超出这个助手的范围。我可以帮你查看睡眠时长、步数和目标记录。"
                continue
            if _UNSUPPORTED.search(clause) and not _NEGATION.search(clause):
                clarification = "目前可以帮你分析睡眠时长、步数和相应目标。"
                continue

            if not conditional and _DATA_PREFACE.fullmatch(clause) and not _NEGATION.search(clause):
                if index + 1 < len(clauses) and clauses[index + 1][0] == sentence_id:
                    try:
                        supplied_window = _windows(clause, today)
                    except ValueError as error:
                        return Route(clarification=str(error))
                    last_metrics = _metrics(clause) or last_metrics
                    last_windows = supplied_window or default_window
                    last_explicit_window = bool(supplied_window)
                    continue

            hits = {intent: match for intent, pattern in _SIGNALS.items() if (match := pattern.search(clause))}
            if Intent.SET_GOAL in hits and "目标" not in clause:
                del hits[Intent.SET_GOAL]
            if Intent.SET_GOAL in hits or Intent.REVIEW in hits:
                hits.pop(Intent.PLAN, None)
            if any(intent in hits for intent in (Intent.EXPLAIN, Intent.REVIEW, Intent.PLAN, Intent.SET_GOAL)):
                if Intent.ANALYZE in hits and hits[Intent.ANALYZE][0] in {"平均", "数据", "记录", "多少", "多久"}:
                    del hits[Intent.ANALYZE]
            explicit_metrics = _metrics(clause)
            metrics = explicit_metrics or last_metrics
            if not hits and explicit_metrics and _WINDOW.search(clause):
                hits[Intent.ANALYZE] = _WINDOW.search(clause)
            if not hits and not conditional and not _NEGATION.search(clause) and metrics and self.model:
                examples = _retrieve(clause)
                decision = self.model.choose(
                    INTENT_SYSTEM,
                    {"text": clause, "recent": context.recent, "examples": examples},
                    INTENT_CHOICES,
                )
                if decision and decision != "unknown":
                    hits[Intent(decision)] = None
            if not hits:
                if _PROMPT.search(clause):
                    clarification = "请说明你希望查询、比较还是复盘什么指标。"
                continue

            try:
                own_windows = _windows(clause, today)
            except ValueError as error:
                clarification = str(error)
                continue
            windows = own_windows or last_windows
            explicit_window = bool(own_windows) or bool(last_windows and last_explicit_window)
            for intent, match in sorted(hits.items(), key=lambda item: item[1].start() if item[1] else 0):
                prefix = clause[:match.end()] if match else clause
                negated = bool(_NEGATION.search(prefix)) and not re.search(r"而是|只(?:看|查|分析|比较)", prefix)
                if negated:
                    status = Status.NEGATED
                elif conditional or intent is Intent.SET_GOAL and re.search(r"明天|下周|之后|稍后|以后|下次", prefix):
                    status = Status.DEFERRED
                elif _BACKGROUND.search(prefix) and not _PROMPT.search(clause):
                    status = Status.BACKGROUND
                else:
                    status = Status.ACTIVE
                if status is not Status.ACTIVE:
                    tasks.append(Task(intent, status, index, metrics, windows))
                    continue

                if intent is Intent.REVIEW and not metrics and len(context.goals) == 1:
                    metrics = tuple(context.goals)
                if not metrics:
                    tasks.append(Task(intent, Status.UNCERTAIN, index))
                    clarification = "你想处理睡眠时长还是步数？"
                    pending = clause
                    continue
                if intent is Intent.SET_GOAL:
                    if re.search(r"怎么|如何|为什么|例如|建议|^(?:是否|可以|能不能).*吗$", clause) or len(metrics) != 1:
                        tasks.append(Task(intent, Status.UNCERTAIN, index, metrics))
                        clarification = "可以直接说『把睡眠目标设为每天 8 小时』或『把步数目标设为每天 8,000 步』。"
                        continue
                    target = _target(clause, metrics[0])
                    if target is None:
                        clarification = "请明确每日目标数值和单位。"
                        continue
                    tasks.append(Task(intent, status, index, metrics, target=target))
                    continue
                uses_personal_data = bool(windows or re.search(r"我的|根据|结合|这个结果|该结果|这些记录", clause))
                if intent in {Intent.EXPLAIN, Intent.PLAN} and not uses_personal_data:
                    task_windows = ()
                else:
                    task_windows = windows or default_window
                show_trend = intent is Intent.ANALYZE and bool(re.search(r"趋势|走势", clause))
                tasks.append(Task(intent, status, index, metrics, task_windows,
                                  show_trend=show_trend, explicit_window=explicit_window))
            active_here = [task for task in tasks if task.clause == index and task.status is Status.ACTIVE]
            if explicit_metrics and active_here:
                last_metrics = explicit_metrics
            for task in active_here:
                if task.windows:
                    last_windows, last_explicit_window = task.windows, task.explicit_window
                    break

        candidates = list(dict.fromkeys([*(task.intent for task in tasks), *candidates]))
        if not clarification and not any(task.status is Status.ACTIVE for task in tasks):
            clarification = "本轮无需执行已否定或延期的任务。" if tasks else "你想查睡眠时长、步数，还是复盘一个现有目标？"
        return Route(tuple(tasks), tuple(candidates), clarification, pending, unsupported_journeys)
