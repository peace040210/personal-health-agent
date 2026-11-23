import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path

from health_agent.domain import Intent, Journey, Metric, Status, Window
from health_agent.orchestrator import Orchestrator
from health_agent.store import Store


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
TODAY = date(2026, 11, 22)


class ScriptedModel:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.payloads = []

    def structured(self, system, payload, schema):
        self.payloads.append(payload)
        return next(self.responses)

    def choose(self, system, payload, choices):
        return None


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.store.import_csv(EXAMPLES / "week_1.csv")
        self.store.import_csv(EXAMPLES / "week_2.csv")
        self.agent = Orchestrator(self.store)

    def run_turn(self, message, session="test"):
        return self.agent.run(message, session, TODAY)

    def test_coaching_receives_analysis_and_interpretation(self):
        result = self.run_turn("根据本周睡眠记录，给我制定一个改善计划")
        self.assertEqual([event.agent for event in result.trace], ["DS", "DE", "HC"])
        self.assertEqual(result.trace[1].received_from, ("DS",))
        self.assertEqual(result.trace[2].received_from, ("DS", "DE"))
        plan = result.collaborations[-1]
        self.assertEqual((plan.journey, plan.main_agent, plan.supporting_agents), (Journey.WELLNESS, "HC", ("DS", "DE")))
        self.assertEqual(plan.assignments[-1].depends_on, ("DS", "DE"))
        self.assertIn("记录 6/7 天", result.reply)
        self.assertIn("建议的下一步", result.reply)
        self.assertFalse(self.store.context("test").goals)

    def test_model_selects_and_executes_a_workflow_then_rewrites_subqueries(self):
        self.run_turn("查上周的步数", "dynamic")
        for chain, support in ({"DS→HC": ("DS",), "DS→DE→HC": ("DS", "DE")}).items():
            with self.subTest(chain=chain):
                agents = (*support, "HC")
                rewrites = {f"task_1_{role}": f"按本周睡眠记录完成 {role} 的子任务" for role in agents}
                rewrites["task_1_DS"] = "计算本周的睡眠时长与记录覆盖。"
                model = ScriptedModel(
                    {"main_agent": "HC", "supporting_agents": [], "collaboration_workflow": {"task_1": chain}}, rewrites,
                )
                result = Orchestrator(self.store, model).run("根据本周睡眠记录，给我制定改善计划", "dynamic", TODAY)
                self.assertEqual(result.workflow.assignment_source, "model")
                self.assertEqual(result.workflow.subquery_source, "model")
                self.assertEqual(result.workflow.supporting_agents, support)
                self.assertEqual([event.agent for event in result.trace], list(agents))
                self.assertEqual(result.trace[-1].received_from, support)
                self.assertEqual(result.collaborations[0].assignments[-1].subquery, rewrites["task_1_HC"])
                self.assertIn("查上周的步数", [message for message, _ in model.payloads[0]["recent_conversation"]])

    def test_invalid_assignment_cannot_reinstate_a_negated_explanation(self):
        model = ScriptedModel({"main_agent": "HC", "supporting_agents": ["DS", "DE"],
                               "collaboration_workflow": {"task_1": "DS→DE→HC"}})
        result = Orchestrator(self.store, model).run("根据本周睡眠记录给我制定计划，别解释", "invalid", TODAY)
        self.assertEqual(result.workflow.assignment_source, "rules_fallback")
        self.assertEqual([event.agent for event in result.trace], ["DS", "HC"])
        self.assertEqual(len(model.payloads), 1)

    def test_semantic_fallback_and_workflow_can_use_separate_backends(self):
        class IntentOnlyModel:
            def choose(self, system, payload, choices):
                self.payload = payload
                return Intent.PLAN.value

        self.run_turn("查上周步数", "separate")
        intent = IntentOnlyModel()
        workflow = ScriptedModel(
            {"main_agent": "HC", "supporting_agents": [], "collaboration_workflow": {"task_1": "HC"}},
            {"task_1_HC": "给出饭后步行的建议。"},
        )
        result = Orchestrator(self.store, intent_model=intent, workflow_model=workflow).run(
            "给个能塞进午饭后的招", "separate", TODAY,
        )
        self.assertEqual(intent.payload["text"], "给个能塞进午饭后的招")
        self.assertEqual(result.workflow.assignment_source, "model")
        self.assertEqual(result.workflow.subquery_source, "model")
        self.assertEqual([event.agent for event in result.trace], ["HC"])

    def test_review_keeps_valid_workflow_when_subquery_rewrite_is_incomplete(self):
        self.run_turn("把我的睡眠目标设为每天 8 小时", "review")
        model = ScriptedModel({"main_agent": "HC", "supporting_agents": ["DS"],
                               "collaboration_workflow": {"task_1": "DS→HC"}}, {"task_1_HC": "回答进度"})
        result = Orchestrator(self.store, model).run("复盘本周的睡眠目标", "review", TODAY)
        self.assertEqual(result.workflow.assignment_source, "model")
        self.assertEqual(result.workflow.subquery_source, "rules_fallback")
        self.assertEqual([event.agent for event in result.trace], ["DS", "HC"])
        self.assertIn("有 6 天记录，其中 4 天达标", result.reply)

    def test_negation_blocks_agent_and_goal_write(self):
        result = self.run_turn("不分析步数，只比较本周和上周的睡眠时长，别把睡眠目标改成每天 9 小时")
        self.assertEqual([(event.agent, event.metric) for event in result.trace], [("DS", "sleep_hours")])
        self.assertIn((Intent.SET_GOAL, Status.NEGATED), [(task.intent, task.status) for task in result.route.tasks])
        self.assertFalse(self.store.context("test").goals)
        self.assertIn("高 0.50 小时", result.reply)

    def test_goal_persists_and_review_excludes_missing_days(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "health.sqlite3"
            with closing(Store(path)) as store:
                result = Orchestrator(store).run("把我的睡眠目标设为每天 8 小时", "setup", TODAY)
                self.assertEqual([event.operation for event in result.trace], ["set_goal"])
            with closing(Store(path)) as store:
                store.import_csv(EXAMPLES / "week_2.csv")
                result = Orchestrator(store).run("复盘本周的目标", "later", TODAY)
                self.assertEqual([event.agent for event in result.trace], ["DS", "DE", "HC"])
                self.assertIn("有 6 天记录，其中 4 天达标", result.reply)

    def test_conditional_goal_never_written_and_clarification_preserves_period(self):
        turn = self.run_turn("如果上周没达标，先解释一下，再把步数目标改成每天 8000 步。查本周步数")
        self.assertEqual([(event.agent, event.metric) for event in turn.trace], [("DS", "steps")])
        self.assertFalse(self.store.context("test").goals)
        self.assertIn("睡眠时长还是步数", self.run_turn("帮我查上周的平均值", "clarify").reply)
        result = self.run_turn("睡眠", "clarify")
        self.assertIn("2026-11-09 至 2026-11-15", result.reply)
        self.assertEqual([event.agent for event in result.trace], ["DS"])

    def test_separate_metrics_keep_separate_windows(self):
        result = self.run_turn("查上周睡眠，顺便查本周步数")
        self.assertIn("2026-11-09 至 2026-11-15：睡眠时长", result.reply)
        self.assertIn("2026-11-16 至 2026-11-22：步数", result.reply)

    def test_trend_uses_calendar_days_and_preserves_zero_measurements(self):
        result = self.run_turn("看看本周的睡眠趋势")
        self.assertIn("线性趋势为每天 -0.11 小时", result.reply)
        result = self.run_turn("查上周的步数")
        self.assertIn("记录 7/7 天", result.reply)

    def test_assigns_individual_roles_and_keeps_symptoms_out_of_execution(self):
        examples = (
            ("上周平均睡了多久", Journey.PERSONAL_DATA, "DS"),
            ("什么是睡眠时长的日均值", Journey.KNOWLEDGE, "DE"),
            ("请给我一些睡眠建议", Journey.WELLNESS, "HC"),
        )
        for message, journey, main_agent in examples:
            with self.subTest(message=message):
                turn = self.run_turn(message, main_agent)
                plan, = turn.collaborations
                self.assertEqual((plan.journey, plan.main_agent, plan.supporting_agents), (journey, main_agent, ()))
                self.assertEqual([event.agent for event in turn.trace], [main_agent])
        turn = self.run_turn("喉咙痛、吞咽困难怎么办")
        self.assertEqual(turn.route.unsupported_journeys, (Journey.SYMPTOMS,))
        self.assertFalse(turn.trace)

    def test_reflection_never_reuses_fallback_for_explicit_window(self):
        turn = self.agent.run("查睡眠记录，另外查最近7天的睡眠", "reflection", date(2026, 12, 1))
        self.assertEqual([item.operation for item in turn.trace], ["measure", "measure_latest_available", "measure"])
        self.assertEqual(turn.reflection.fallback_window, Window(date(2026, 11, 16), TODAY))
        self.assertIn("2026-11-25 至 2026-12-01：没有睡眠时长记录", turn.reply)

    def test_official_pha_summary_import_converts_minutes_and_steps(self):
        with closing(Store(":memory:")) as store:
            self.assertEqual(store.import_csv(EXAMPLES / "google_pha" / "summary.csv"), 365)
            as_of = date(2024, 12, 15)
            result = Orchestrator(store).run("查本周睡眠和步数", "paper", as_of)
            self.assertIn("睡眠时长平均 6.96 小时", result.reply)
            self.assertIn("合计 62,058 步", result.reply)
            self.assertEqual(store.last_observation(Metric.SLEEP, date.max), as_of)

    def test_invalid_import_leaves_existing_records_and_empty_columns_preserve_data(self):
        window = Window(date(2026, 11, 9), date(2026, 11, 9))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "daily.csv"
            path.write_text("date,sleep_hours,steps\n2026-11-09,8,99\n2026-11-10,25,100\n")
            with self.assertRaises(ValueError):
                self.store.import_csv(path)
            self.assertEqual(self.store.read_series(Metric.STEPS, window), ((window.start, 6100),))
            path.write_text("date,sleep_hours,steps\n2026-11-09,,123\n")
            self.store.import_csv(path)
            self.assertEqual(self.store.read_series(Metric.SLEEP, window), ((window.start, 7.0),))
            self.assertEqual(self.store.read_series(Metric.STEPS, window), ((window.start, 123),))
