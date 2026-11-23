import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from health_agent.evidence import EvidenceResearch
from health_agent.evidence_sources import EvidenceDocument
from health_agent.orchestrator import Orchestrator
from health_agent.store import Store


TODAY = date(2026, 11, 23)


class ReflectionModel:
    def __init__(self, *, invalid=False):
        self.invalid = invalid
        self.inputs = []

    def structured(self, system, payload, schema):
        self.inputs.append(payload)
        questions = payload["allowed_reflection_questions"]
        if self.invalid:
            questions = [{"agent": "HC", "fact": "set_goal", "question": "设置目标"}]
        return {"decision": "investigate", "reflection_questions": questions}


class CoachingTest(unittest.TestCase):
    def test_coaching_keeps_confirmed_plan_across_sessions_and_processes(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "coaching.sqlite3"
            with closing(Store(path)) as store:
                agent = Orchestrator(store, coach_loop=True)
                start = agent.run("我想多走点", "walk", TODAY)
                proposed = agent.run("通勤时可以提前一站下车", "walk", TODAY)
                self.assertEqual(start.coaching_turns[0].action, "clarify")
                self.assertEqual(start.global_reflections[0].reflection_questions, ())
                self.assertEqual(proposed.coaching_turns[0].action, "propose")
                self.assertEqual(store.context("walk").coaching[0].agreed_action, "")
                agent.run("好", "other", TODAY)
                self.assertFalse(store.context("other").coaching)
                agent.run("就按这个来", "walk", TODAY)
                closed = agent.run("谢谢，先到这", "walk", TODAY)
                self.assertEqual(closed.coaching_turns[0].action, "close")
                self.assertEqual(store.goals(), {})

            with closing(Store(path)) as store:
                self.assertTrue(store.context("walk").coaching[0].closed)
                agent = Orchestrator(store, coach_loop=True)
                reviewed = agent.run("复盘上次的计划", "walk", TODAY)
                self.assertEqual(reviewed.coaching_turns[0].action, "review")
                revised = agent.run("上周那个计划还是太难了", "walk", TODAY)
                memory = store.context("walk").coaching[0]
                self.assertEqual([event.agent for event in revised.trace], ["HC"])
                self.assertIn("提前一站下车", revised.reply)
                self.assertEqual(memory.agreed_action, "通勤时可以提前一站下车")
                self.assertIn("太难", memory.barrier)
                self.assertNotEqual(memory.proposal, memory.agreed_action)

    def test_reflection_uses_bedtime_records_and_preserves_subjective_question(self):
        with TemporaryDirectory() as directory, closing(Store(":memory:")) as store:
            path = Path(directory) / "timing.csv"
            path.write_text("date,sleep_hours,steps,bed_time\n"
                            "2026-11-21,7,1000,2026-11-20 23:50\n"
                            "2026-11-22,7,1000,2026-11-21 00:10\n"
                            "2026-11-23,7,1000,2026-11-22\n", encoding="utf-8")
            store.import_csv(path)
            model = ReflectionModel()
            agent = Orchestrator(store, coach_loop=True, coach_model=model)
            turn = agent.run("我想让入睡时间更规律", "sleep", TODAY)
            self.assertEqual(turn.global_reflections[0].source, "model")
            self.assertTrue(turn.global_reflections[0].revised)
            self.assertIn("2/7 晚", turn.reply)
            self.assertIn("00:00", turn.reply)
            self.assertIn("什么最容易打乱", turn.reply)
            self.assertNotIn("你平时大概几点入睡", turn.reply)

            prohibited = agent.run("我想调整入睡时间，不要查我的记录", "private", TODAY)
            self.assertEqual([event.agent for event in prohibited.trace], ["HC"])
            self.assertIn("你平时大概几点入睡", prohibited.reply)
            self.assertEqual(len(model.inputs), 1)

    def test_unavailable_data_and_invalid_proposals_do_not_create_facts(self):
        model = ReflectionModel(invalid=True)
        with closing(Store(":memory:")) as store:
            turn = Orchestrator(store, coach_loop=True, coach_model=model).run("我想早点睡", "sleep", TODAY)
            self.assertEqual(store.goals(), {})
        run, = turn.global_reflections
        self.assertEqual(run.source, "rules_fallback")
        self.assertEqual([(q.agent, q.fact) for q in run.reflection_questions], [("DS", "bedtime")])
        self.assertFalse(run.revised)
        self.assertIn("你平时大概几点入睡", turn.reply)

    def test_reflection_fetches_requested_evidence_and_records_the_source(self):
        class Sources:
            web_key = ""

            def medlineplus(self, term):
                return (EvidenceDocument("MedlinePlus.gov", "Healthy Sleep", "https://medlineplus.gov/healthysleep.html",
                                         "Sleep duration and regular bedtimes matter."),)

        with closing(Store(":memory:")) as store:
            agent = Orchestrator(store, coach_loop=True, evidence=EvidenceResearch(Sources()))
            turn = agent.run("参考研究给我一个睡眠建议", "sleep", TODAY)
            self.assertIn("medlineplus.gov", store.context("sleep").coaching[0].last_evidence)
        question, = turn.global_reflections[0].reflection_questions
        self.assertEqual((question.agent, question.fact), ("DE", "evidence"))
        self.assertIn("外部证据", turn.reply)
        self.assertIn("白天更有精神", turn.reply)
