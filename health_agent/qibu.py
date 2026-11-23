"""User-facing Agent decisions and the database tools that execute them."""

import json
import re
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .domain import Metric, Window
from .evidence import EvidenceResearch, EvidenceScope
from .model import WorkflowModel
from .orchestrator import Orchestrator
from .router import _number, _windows
from .skills import SkillRegistry
from .store import Store

_LABELS = {"nap": "午休", "walk": "散步", "rest": "休息", "sleep": "睡前放松"}
_METRICS = {"sleep_hours": "睡眠", "deep_sleep_hours": "深睡", "steps": "步数"}
_SLEEP_LINK = "https://www.cdc.gov/sleep/about/index.html"
_PROMPT = """你是栖步的意图与参数提取器，只输出 JSON。
records 查用户睡眠、深睡或步数，compare 查步数与睡眠的关系，knowledge 问公共健康知识。
action 是拟定未来行动，表达已做过的事属于 acknowledge。coach 是讨论习惯或限制、尚未给出具体行动。
close 是结束话题，unknown 是不能理解或超范围。不能输出答案、数值计算、诊断或执行写入。
参数根据用户原话提取。信息不足留空。日期由程序解析。用户提到今天、昨晚时，单晚查记录，不要改成多天。
提议行动时，minutes 只来自用户明确给出的时长。周计划才选 week。显式未来日期选 other。
用户说“那深睡呢”可以继承 last_query 的日期。用户说“改成十五分钟”可以继承 pending_action。
用户说婴儿、月龄、几岁该睡多久，属于 knowledge。健康知识只选匹配的 area，超出范围选 other。
即便用户要求立即保存，也只提取参数。保存由后端确认操作执行。"""


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Literal["records", "compare", "knowledge", "action", "coach", "acknowledge", "close", "unknown"]
    metric: Literal["sleep_hours", "deep_sleep_hours", "steps"] | None = None
    area: Literal["sleep_by_age", "sleep_stages", "sleep_quality", "activity", "other"] | None = None
    action: Literal["nap", "walk", "rest", "sleep"] | None = None
    minutes: int | None = Field(default=None, ge=1, le=1440, strict=True)
    cadence: Literal["today", "week", "other"] | None = None


class ActionConflict(ValueError):
    pass


@dataclass(frozen=True)
class Reply:
    title: str
    lines: tuple[str, ...] = ()
    intent: str = "unknown"
    understanding: str = "rules"
    agents: tuple[str, ...] = ()
    sources: tuple[dict, ...] = ()
    draft: dict | None = None
    memory_draft: dict | None = None


class QibuTools:
    """Per-session SQLite backed query state and explicit action operations."""

    def __init__(self, store: Store):
        self.store = store
        self.db = store.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS qibu_context (
                id INTEGER PRIMARY KEY CHECK (id = 1), value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS qibu_actions (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, minutes INTEGER NOT NULL,
                day TEXT NOT NULL, days INTEGER NOT NULL, state TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS qibu_checkins (
                action_id TEXT NOT NULL REFERENCES qibu_actions(id), day TEXT NOT NULL,
                PRIMARY KEY (action_id, day)
            );
            CREATE TABLE IF NOT EXISTS qibu_history (
                id INTEGER PRIMARY KEY, message TEXT NOT NULL, reply TEXT NOT NULL, turn_id TEXT
            );
        """)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if not any(column[1] == "turn_id" for column in self.db.execute("PRAGMA table_info(qibu_history)")):
                self.db.execute("ALTER TABLE qibu_history ADD COLUMN turn_id TEXT")
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS qibu_history_turn ON qibu_history(turn_id)")

    def context(self) -> dict:
        row = self.db.execute("SELECT value FROM qibu_context WHERE id = 1").fetchone()
        return json.loads(row[0]) if row else {}

    def remember(self, state: dict) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO qibu_context VALUES (1, ?)", (json.dumps(state),))

    def record(self, message: str, reply: dict, turn_id: str | None) -> None:
        with self.db:
            self.db.execute("INSERT INTO qibu_history(message, reply, turn_id) VALUES (?, ?, ?)",
                            (message, json.dumps(reply, ensure_ascii=False), turn_id))

    def previous_reply(self, message: str, turn_id: str | None) -> dict | None:
        if turn_id is None:
            return None
        row = self.db.execute("SELECT message, reply FROM qibu_history WHERE turn_id = ?", (turn_id,)).fetchone()
        if row is None:
            return None
        if row[0] != message:
            raise ActionConflict("这次重试的消息已改变，请重新发送。")
        return json.loads(row[1])

    def reply_state(self, reply: dict, today: date, memory_status: dict[str, str]) -> dict:
        if draft := reply.get("draft"):
            current = self._action(draft["id"])
            if current and (current["state"] != "draft" or current["day"] == today.isoformat()):
                reply["draft_status"] = current["state"]
            else:
                reply["draft_status"] = "expired"
        if draft := reply.get("memory_draft"):
            reply["memory_status"] = memory_status.get(draft["id"], "expired")
        return reply

    def history(self, today: date, memory_status: dict[str, str]) -> list[dict]:
        history = []
        for message, raw in self.db.execute(
                "SELECT message, reply FROM (SELECT * FROM qibu_history ORDER BY id DESC LIMIT 100) ORDER BY id"):
            history.append({"message": message, "reply": self.reply_state(json.loads(raw), today, memory_status)})
        return history

    def current_draft_id(self, today: date) -> str | None:
        row = self.db.execute("SELECT id FROM qibu_actions WHERE state = 'draft' AND day = ?",
                              (today.isoformat(),)).fetchone()
        return row[0] if row else None

    def _action(self, action_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT id, kind, minutes, day, days, state FROM qibu_actions WHERE id = ?", (action_id,)
        ).fetchone()
        if not row:
            return None
        item = dict(zip(("id", "kind", "minutes", "day", "days", "state"), row))
        item["label"] = f"{_LABELS[item['kind']]} {item['minutes']} 分钟"
        item["checked"] = [day for day, in self.db.execute(
            "SELECT day FROM qibu_checkins WHERE action_id = ? ORDER BY day", (action_id,)
        )]
        return item

    def abandon_draft(self) -> None:
        with self.db:
            self.db.execute("UPDATE qibu_actions SET state = 'cancelled' WHERE state = 'draft'")

    def propose(self, kind: str, minutes: int, today: date, days: int) -> dict:
        action_id = str(uuid4())
        with self.db:
            self.db.execute("UPDATE qibu_actions SET state = 'cancelled' WHERE state = 'draft'")
            self.db.execute("INSERT INTO qibu_actions VALUES (?, ?, ?, ?, ?, 'draft')",
                            (action_id, kind, minutes, today.isoformat(), days))
        return self._action(action_id)

    def actions(self, today: date) -> list[dict]:
        ids = [item[0] for item in self.db.execute("""
            SELECT id FROM qibu_actions WHERE state = 'active'
            AND day <= ? AND date(day, '+' || (days - 1) || ' days') >= ? ORDER BY rowid
        """, (today.isoformat(), today.isoformat()))]
        return [self._action(action_id) for action_id in ids]

    def change(self, action_id: str, operation: str, today: date, day: date | None = None) -> dict:
        item = self._action(action_id)
        if not item:
            raise LookupError("找不到这项行动。")
        start = date.fromisoformat(item["day"])
        if operation == "confirm":
            if item["state"] == "active":
                return item
            if item["state"] != "draft" or start != today:
                raise ActionConflict("这项提议已经更新或过期，请重新提议。")
            with self.db:
                self.db.execute("UPDATE qibu_actions SET state = 'active' WHERE id = ?", (action_id,))
        elif operation == "remove":
            with self.db:
                self.db.execute("UPDATE qibu_actions SET state = 'cancelled' WHERE id = ?", (action_id,))
        else:
            if (item["state"] != "active" or day is None or day > today
                    or not start <= day < start + timedelta(days=item["days"])):
                raise ActionConflict("请选择这项行动中已到达的日期。")
            with self.db:
                if operation == "complete":
                    self.db.execute("INSERT OR IGNORE INTO qibu_checkins VALUES (?, ?)", (action_id, day.isoformat()))
                else:
                    self.db.execute("DELETE FROM qibu_checkins WHERE action_id = ? AND day = ?", (action_id, day.isoformat()))
        return self._action(action_id)

    def measurements(self, metric: str, window: Window) -> tuple[float, ...]:
        if metric == "steps":
            return tuple(value for _, value in self.store.read_series(Metric.STEPS, window))
        start, end = window.start.isoformat(), window.end.isoformat()
        return tuple(row[metric] for row in self.store.analysis_rows(window)
                     if row["bedtime_day"] and start <= row["bedtime_day"] <= end and row[metric] is not None)


_DURATION = r"([0-9]+|[零一二两三四五六七八九十]+)\s*分钟"
_ABS_DATE = r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)"
_NEGATED = r"不想|不要|别安排|不用|取消|不需要"
_MEDICAL = r"诊断|确诊|处方|用药|吃什么药|安眠药|急救|胸痛|呼吸困难|昏厥"
_LUNCH = r"午休|午睡|午饭后|中午"
_RECALL_REQUEST = r"按我平时|按我之前|按我的时间|按工作日|按午休时间"


def _minutes(text: str) -> int | None:
    if match := re.search(_DURATION, text):
        value = _number(match[1])
        return value if 1 <= value <= 1440 else None
    if "半小时" in text or "半个小时" in text:
        return 30
    return 15 if "一刻钟" in text else None


def lunch_budget_declaration(text: str) -> int | None:
    if (re.search(r"工作日|上班日|周一到周五", text) and re.search(_LUNCH, text)
            and re.search(r"只有|只能|最多|不超过|不多于|压缩到|改成", text)
            and not re.search(r"今天|明天|昨天|这次|本周|这周|临时|如果|假如|吗|[？?]|不再|不用|不要", text)
            and not re.search(r"我(?:的)?(?:朋友|同事|家人|孩子)|他们|她们|他工作日|她工作日", text)):
        return _minutes(text)
    return None


def _metric(text: str) -> str | None:
    if re.search(r"深睡|深度睡眠", text):
        return "deep_sleep_hours"
    if re.search(r"睡眠|睡了|睡得|睡多久|睡几|睡了没|哪晚睡|几点睡|入睡", text):
        return "sleep_hours"
    if re.search(r"步数|走(?:了|得|多少|几步)", text):
        return "steps"
    return None


def _action_kind(text: str) -> str | None:
    for match, kind in ((r"午休|午睡|小睡|眯一会", "nap"), (r"散步|步行|走一走|走路", "walk"),
                        (r"睡前|早睡", "sleep"), (r"休息", "rest")):
        if re.search(match, text):
            return kind
    return None


def _fallback(text: str, state: dict) -> Decision:
    metric, action, minutes = _metric(text), _action_kind(text), _minutes(text)
    if re.fullmatch(r"(没什么|没事|不用了|不需要了|谢谢|先这样|先到这|(?:还是)?先不安排了)[。！!？?]*", text):
        return Decision(intent="close")
    if "睡" in text and re.search(r"婴儿|宝宝|我家娃|新生儿|儿童|青少年|成人|老人|月龄|岁|合适|应该睡|需要睡", text):
        return Decision(intent="knowledge", area="sleep_by_age")
    if re.search(r"已经|刚刚|刚才", text) and action:
        return Decision(intent="acknowledge")
    if (action and re.search(r"需要|想|打算|准备|只有|只能|帮我|安排|今天|中午|待会", text)
            and not re.search(r"数据|记录|我昨天|睡了|走了|多久", text)
            or state.get("pending_action") and (minutes is not None or re.search(_NEGATED, text))):
        cadence = "other" if re.search(r"明天|明早|后天|下周", text) else "week" if re.search(r"一周|每周|每天|7天|七天", text) else None
        return Decision(intent="action", action=action, cadence=cadence, minutes=minutes)
    if re.search(r"走得|步数高|步数低", text) and metric and metric != "steps":
        return Decision(intent="compare", metric=metric)
    if re.search(r"什么是|什么意思|区别|为什么|怎么回事|有何含义", text):
        area = {"deep_sleep_hours": "sleep_stages", "sleep_hours": "sleep_quality", "steps": "activity"}.get(metric, "other")
        return Decision(intent="knowledge", area=area)
    if action == "nap" and re.search(r"昨天|记录|睡了|多久", text):
        return Decision(intent="acknowledge")
    if metric or state.get("last_query") and re.search(r"昨晚|昨天|前晚|上周|本周|数据|记录|呢", text):
        return Decision(intent="records", metric=metric)
    if re.search(r"计划|目标|习惯|建议|困扰|坚持", text):
        return Decision(intent="coach")
    return Decision(intent="unknown")


def _window(text: str, as_of: date, previous: dict | None) -> Window | None:
    dates = re.findall(_ABS_DATE, text)
    if not dates:
        pattern = r"(?:(\d{4})年)?(\d{1,2})[月/](\d{1,2})(?:日|号)?"
        dates = [f"{year or as_of.year}-{int(month):02d}-{int(day):02d}" for year, month, day in re.findall(pattern, text)]
    if dates:
        days = [date.fromisoformat(day) for day in dates]
        if len(days) > 2 or len(days) == 2 and not re.search(r"到|至|~", text):
            raise ValueError("请给出一个日期，或明确写出起止日期。")
        window = Window(days[0], days[-1])
        if not 1 <= window.days <= 366:
            raise ValueError("请按先后顺序给出 1 至 366 天的查询范围。")
        return window
    text = re.sub(r"昨夜", "昨晚", text)
    if re.search(r"前天|前晚|明早|明晚|明天|后天", text):
        offset = -2 if re.search(r"前天|前晚", text) else 2 if "后天" in text else 1
        day = as_of + timedelta(days=offset)
        return Window(day, day)
    windows = _windows(text, as_of)
    if len(windows) > 1:
        raise ValueError("可以先查一个日期范围。")
    if windows:
        return windows[0]
    if previous and previous.get("start") and (re.search(r"那|呢|再看|也看|^深睡$|^步数$|^睡眠$", text) or not previous.get("metric")):
        return Window(date.fromisoformat(previous["start"]), date.fromisoformat(previous["end"]))
    return None


def _duration(hours: float) -> str:
    total = round(hours * 60)
    h, m = divmod(total, 60)
    return " ".join(part for part in (f"{h} 小时" if h else "", f"{m} 分" if m or not h else "") if part)


def _age_guidance(text: str) -> tuple[str, ...] | None:
    if match := re.search(r"([0-9]+|[一二两三四五六七八九十]+)\s*(?:个)?\s*(月|岁)", text):
        number = _number(match[1])
        if match[2] == "月" and number <= 12:
            return ("0–3 个月每天 14–17 小时。",) if number < 4 else ("4–12 个月每天 12–16 小时，含小睡。",)
        years = number if match[2] == "岁" else number // 12
        ranges = ((2, "11–14 小时，含小睡"), (5, "10–13 小时，含小睡"), (12, "9–12 小时"),
                  (17, "8–10 小时"), (60, "至少 7 小时"), (64, "7–9 小时"), (120, "7–8 小时"))
        return next(((f"{years} 岁每天建议睡 {hours}。",) for bound, hours in ranges if 1 <= years <= bound), None)
    if re.search(r"婴儿|宝宝|新生儿", text):
        return ("0–3 个月每天 14–17 小时。", "4–12 个月每天 12–16 小时，含小睡。", "宝宝几个月大？")
    return None


class QibuAgent:
    def __init__(self, tools: QibuTools, core: Orchestrator, evidence: EvidenceResearch,
                 skills: SkillRegistry, model: WorkflowModel | None, lunch_budget: int | None = None):
        self.tools, self.core, self.evidence, self.skills, self.model = tools, core, evidence, skills, model
        self.lunch_budget = lunch_budget

    def understand(self, text: str, state: dict) -> tuple[Decision, str]:
        fallback = _fallback(text, state)
        if fallback.intent == "close":
            return fallback, "rules"
        if self.model:
            result = self.model.structured(_PROMPT, {"message": text, **state}, Decision.model_json_schema(), max_tokens=260)
            try:
                return Decision.model_validate(result), "model"
            except ValidationError:
                return fallback, "fallback"
        return fallback, "rules"

    def run(self, text: str, session: str, as_of: date, today: date) -> Reply:
        state = self.tools.context()
        if re.search(_MEDICAL, text):
            return Reply("这个问题适合向专业人士咨询", ("我可以提供有出处的日常健康信息，也可以帮你查看记录。",))
        decision, understanding = self.understand(text, state)
        # A supplied age answers an existing age question even when a model is unavailable.
        if state.get("awaiting_age") and re.fullmatch(r"(?:刚满|满)?[0-9一二两三四五六七八九十]+\s*(?:个)?[月岁](?:大|了)?[。！!？?]*", text):
            decision = Decision(intent="knowledge", area="sleep_by_age")
        pending_action = state.get("pending_action") or {}
        if (decision.intent in {"coach", "unknown"} and self.lunch_budget
                and (re.search(_LUNCH, text) or (_action_kind(text) or pending_action.get("kind")) == "nap")
                and re.search(_RECALL_REQUEST, text)):
            decision = Decision(intent="action")
            understanding = "rules"
        reply, new_state = self._respond(decision, text, state, session, as_of, today)
        self.tools.remember(new_state)
        return Reply(**(asdict(reply) | {"intent": decision.intent, "understanding": understanding}))

    def _respond(self, decision: Decision, text: str, state: dict, session: str,
                 as_of: date, today: date) -> tuple[Reply, dict]:
        if decision.intent == "close":
            self.tools.abandon_draft()
            return Reply("好的，先聊到这里。"), {}
        if decision.intent == "action":
            return self._draft_action(decision, text, state, today)
        if decision.intent == "records":
            return self._read_records(decision, text, state, as_of)
        if decision.intent == "knowledge":
            return self._explain(decision, text, state)
        if decision.intent == "acknowledge":
            if re.search(r"查|数据|多久|几分钟|记录", text) and _action_kind(text) == "nap":
                return Reply("样例数据里没有单独的午休记录"), state
            return Reply("我明白了。", ("如果想标记一项已安排的行动，可以打开「我的计划」。",)), state
        if decision.intent in {"compare", "coach"}:
            turn = self.core.run(text, session, as_of)
            agents = tuple(dict.fromkeys(item.agent for item in turn.trace))
            return Reply("数据分析" if decision.intent == "compare" else "一起安排下一步",
                         (turn.reply or "你想改善睡眠，还是增加一些日常步行？",), agents=agents), {}
        return Reply("你想查记录、了解健康知识，还是安排一项行动？",
                     ("可以告诉我具体的指标、日期或你想做的事。",)), state

    def _draft_action(self, decision: Decision, text: str, state: dict, today: date) -> tuple[Reply, dict]:
        if re.search(_NEGATED, text):
            self.tools.abandon_draft()
            return Reply("可以先不安排。"), {}
        if re.search(r"已经|刚刚|刚才|昨天|昨晚|前天", text):
            return Reply("这听起来是已经发生的事。",
                         ("如果想打卡，可以在「我的计划」标记已安排的行动。",)), state
        pending = state.get("pending_action") or {}
        action = _action_kind(text) or decision.action or pending.get("kind")
        explicit_date = re.search(_ABS_DATE, text)
        explicit_today = re.search(r"今天|今日|待会|等会", text) or (explicit_date and explicit_date[0] == today.isoformat())
        if (re.search(r"明天|明早|明晚|后天|下周", text) or (explicit_date and explicit_date[0] != today.isoformat())
                or (decision.cadence == "other" and not explicit_today)):
            return Reply("请选择今天的一次行动，或从今天开始的一周计划。"), {}
        if re.search(r"一周|每周|每天|七天|7\s*天", text):
            days = 7
        elif explicit_today:
            days = 1
        else:
            days = pending.get("days", 7 if decision.cadence == "week" else 1)
        if not action:
            return Reply("你想安排午休、散步，还是睡前放松？"), {"pending_action": {"days": days}}
        # Recover the reported duration from the actual words to avoid inventing one.
        minutes = _minutes(text)
        recalled = False
        lunch_context = re.search(_LUNCH, text) or (action == "nap" and re.search(_RECALL_REQUEST, text))
        if (minutes is None and not re.search(_DURATION, text) and self.lunch_budget
                and action in {"nap", "walk", "rest"} and days == 1 and today.weekday() < 5 and lunch_context):
            minutes, recalled = self.lunch_budget, True
        if not minutes:
            self.tools.abandon_draft()
            return Reply(f"想安排几分钟{_LABELS[action]}？"), {"pending_action": {"kind": action, "days": days}}
        draft = self.tools.propose(action, minutes, today, days)
        label = "一周计划" if days == 7 else "今日行动"
        details = ("已使用你确认过的工作日午休时间限制。",) if recalled else ()
        return Reply(f"{_LABELS[action]} {minutes} 分钟", (*details, f"确认后加入{label}。"), agents=("HC",), draft=draft), {
            "pending_action": {"kind": action, "days": days},
        }

    def _read_records(self, decision: Decision, text: str, state: dict, as_of: date) -> tuple[Reply, dict]:
        previous = state.get("last_query")
        metric = _metric(text) or decision.metric or (previous or {}).get("metric")
        if metric is None and re.search(r"昨晚|昨天晚上|前晚|昨夜", text):
            metric = "sleep_hours"
        try:
            window = _window(text, as_of, previous)
        except ValueError as error:
            return Reply("需要确认日期", (str(error),)), state
        if not metric:
            pending = {"last_query": {"start": window.start.isoformat(), "end": window.end.isoformat()}} if window else state
            return Reply("你想查步数、睡眠，还是深睡？"), pending
        if not window:
            return Reply(f"想查哪一天或哪段时间的{_METRICS[metric]}？"), {"last_query": {"metric": metric}}
        new_state = {"last_query": {"metric": metric, "start": window.start.isoformat(), "end": window.end.isoformat()}}
        if metric == "steps" and re.search(r"晚|夜间", text):
            return Reply("样例步数按整天记录", ("可以查同一天的全天步数。",)), new_state
        values = self.tools.measurements(metric, window)
        source = f"样例日期 {window.start}" if window.days == 1 else f"样例日期 {window.start} 至 {window.end}"
        if not values:
            return Reply("这个日期范围没有相关记录", (source + "。",), agents=("DS",)), new_state
        if window.days == 1 and len(values) > 1:
            return Reply("这一天有多条记录", (source + "。需要先确认要看哪条。",), agents=("DS",)), new_state
        total = metric == "steps" and not re.search(r"平均|日均", text) and bool(re.search(r"一共|总共|合计|总步数|走了多少", text))
        value = sum(values) if total else sum(values) / len(values)
        display = f"{round(value):,} 步" if metric == "steps" else _duration(value)
        label = ("全天" if metric == "steps" else "当晚") + _METRICS[metric] if window.days == 1 else _METRICS[metric] + ("合计" if total else "日均")
        alignment = "，按入睡日查询" if metric != "steps" else ""
        lines = [f"{source}{alignment}，有 {len(values)} 条记录。"]
        if window.days == 1 and metric != "steps":
            if bedtime := self.tools.store.bedtime_minutes(window):
                hour, minute = divmod(bedtime[0], 60)
                lines.append(f"入睡时间 {hour:02d}:{minute:02d}。")
            if metric == "sleep_hours" and len(deep := self.tools.measurements("deep_sleep_hours", window)) == 1:
                lines.append("深睡 " + _duration(deep[0]) + "。")
        return Reply(f"{label} {display}", tuple(lines), agents=("DS",)), new_state

    def _explain(self, decision: Decision, text: str, state: dict) -> tuple[Reply, dict]:
        if decision.area == "sleep_by_age":
            lines = _age_guidance(text)
            if lines is None:
                return Reply("你问的是几个月或几岁的人？"), {"awaiting_age": True}
            return Reply("按年龄的睡眠时长建议", lines, agents=("DE",),
                         sources=({"title": "CDC 睡眠时长建议", "url": _SLEEP_LINK},)), {
                "awaiting_age": len(lines) > 1,
            }
        if decision.area not in {"sleep_stages", "sleep_quality", "activity"}:
            return Reply("这个问题缺少可核实的资料",
                         ("可以具体说说你想了解的是睡眠时长、睡眠分期还是日常活动。",)), {}
        metric = Metric.STEPS if decision.area == "activity" else Metric.SLEEP
        outcome = "deep_sleep_hours" if decision.area == "sleep_stages" else None
        scope = EvidenceScope.from_question(metric, text, outcome=outcome)
        result = self.evidence.run(scope, skill=self.skills.start().activate("DE"))
        if not result.findings:
            return Reply("这轮没有查到可核实的资料", tuple(result.limitations), agents=("DE",)), {}
        return Reply("健康资料", tuple(item.quote for item in result.findings if item.quote),
                     agents=("DE",), sources=tuple({"title": item.title, "url": item.url} for item in result.findings)), {}
