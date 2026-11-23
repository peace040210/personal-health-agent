import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from health_agent.ds_sandbox import PythonSandbox
from health_agent.evidence import EvidenceResearch
from health_agent.evidence_sources import EvidenceDocument
from health_agent.orchestrator import Orchestrator
from health_agent.skills import SkillRegistry
from health_agent.store import Store


_GOOD_CODE = '''def analysis(rows, plan):
    start = plan["start"]
    end = plan["end"]
    alignment = plan["alignment"]
    field = plan["sleep_metric"]
    steps = {row["day"]: row["steps"] for row in rows if row["steps"] is not None and start <= row["day"] <= end}
    sleep = {row[alignment]: row[field] for row in rows if row[field] is not None}
    days = [day for day in steps if day in sleep]
    distances = [steps[day] for day in days]
    if plan["threshold"] == "goal":
        cutoff = plan["step_goal"]
        high = [day for day in days if steps[day] >= cutoff]
    else:
        cutoff = median(distances)
        high = [day for day in days if steps[day] > cutoff]
    others = [day for day in days if day not in high]
    high_hours = [sleep[day] for day in high]
    other_hours = [sleep[day] for day in others]
    high_mean = mean(high_hours)
    other_mean = mean(other_hours)
    return {"paired_days": len(days), "step_cutoff": cutoff, "high_days": len(high), "other_days": len(others),
            "high_mean_hours": high_mean, "other_mean_hours": other_mean, "difference_hours": high_mean - other_mean}
'''
_AS_OF = date(2026, 11, 6)
_QUESTION = "过去6天，步数高的日子，当晚的睡眠时长更长吗？"


class ScriptedAnalysis:
    def __init__(self, *codes, review=None):
        self.codes = iter(codes)
        self.review = review or {"decision": "summary", "feedback": ""}
        self.feedback = []
        self.requests = []
        self.system_prompts = []

    def structured(self, system, payload, schema, *, max_tokens=512):
        self.requests.append(payload.copy())
        self.system_prompts.append(system)
        if "scope" in payload:
            return {**payload["scope"], "minimum_per_group": 2, "approach": "按入睡日配对步数与睡眠。"}
        if "result" in payload:
            return self.review
        if "previous_attempt" in payload:
            self.feedback.append(payload["previous_attempt"])
        code = next(self.codes, None)
        return {"code": code} if code is not None else None


def _load_days(store):
    with TemporaryDirectory() as directory:
        import_file = Path(directory) / "daily.csv"
        import_file.write_text("date,sleep_hours,steps,bed_time,deep_sleep_minutes\n"
                               "2026-11-01,,1000,,\n"
                               "2026-11-02,4,3000,2026-11-01 22:00:00,60\n"
                               "2026-11-03,5,5000,2026-11-02 22:00:00,60\n"
                               "2026-11-04,6,7000,2026-11-03 22:00:00,120\n"
                               "2026-11-05,7,9000,2026-11-04 22:00:00,120\n"
                               "2026-11-06,8,11000,2026-11-05 22:00:00,180\n"
                               "2026-11-07,9,13000,2026-11-06 22:00:00,180\n", encoding="utf-8")
        store.import_csv(import_file)


@unittest.skipUnless(PythonSandbox().available, "requires the local macOS sandbox")
class SandboxTest(unittest.TestCase):
    def test_feeds_joint_analysis_and_evidence_to_coach_when_requested(self):
        class Sources:
            web_key = ""

            def pubmed(self, term):
                return (EvidenceDocument("PubMed", "Daily steps and sleep duration",
                                         "https://pubmed.ncbi.nlm.nih.gov/123/",
                                         "The study measured daily steps and sleep duration the following night."),)

        model = ScriptedAnalysis(_GOOD_CODE)
        with closing(Store(":memory:")) as store:
            _load_days(store)
            agent = Orchestrator(store, ds_model=model, evidence=EvidenceResearch(Sources()), skills=SkillRegistry())
            turn = agent.run(_QUESTION + "然后给我一个建议", "demo", _AS_OF)
            negated = agent.run(_QUESTION.rstrip("？") + "，别查外部证据", "demo", _AS_OF)
        self.assertEqual([event.agent for event in turn.trace], ["DS", "DE", "HC"])
        self.assertEqual(turn.trace[2].received_from, ("DS", "DE"))
        self.assertEqual([(event.role, event.version) for event in turn.skill_events if event.stage == "activate"],
                         [("DS", "v1"), ("DE", "v1"), ("HC", "v1")])
        self.assertEqual([(event.role, event.resource) for event in turn.skill_events if event.stage == "reference"],
                         [("DE", "policy.json")])
        self.assertIn("有效配对日", turn.evidence_runs[0].personal_observation)
        self.assertEqual(turn.evidence_runs[0].coach_context()["personal_analysis"]["statistics"]["paired_days"], 6)
        self.assertIn("周末按同样口径", turn.reply)
        self.assertFalse(negated.evidence_runs)
        self.assertEqual([event.agent for event in negated.trace], ["DS"])

    def test_repairs_execution_and_date_alignment_errors(self):
        broken = _GOOD_CODE.replace('row["day"]', 'row["date"]')
        bad_join = _GOOD_CODE.replace('alignment = plan["alignment"]', 'alignment = "day"')
        model = ScriptedAnalysis(broken, bad_join, _GOOD_CODE)
        with closing(Store(":memory:")) as store:
            _load_days(store)
            turn = Orchestrator(store, ds_model=model, skills=SkillRegistry()).run(_QUESTION, "demo", _AS_OF)
        analysis, = turn.sandbox_analyses
        self.assertEqual([attempt.status for attempt in analysis.attempts], ["execution_error", "result_error", "verified"])
        self.assertIn("KeyError", model.feedback[0]["feedback"])
        self.assertIn("date alignment", model.feedback[1]["feedback"])
        self.assertEqual(analysis.result["paired_days"], 6)
        self.assertEqual(analysis.result["step_cutoff"], 6000)
        self.assertEqual(analysis.result["difference_hours"], 3)
        self.assertEqual(len(turn.collaborations), 1)
        self.assertTrue(all("rows" not in request for request in model.requests))
        self.assertNotIn("针对上一版的具体反馈", model.system_prompts[1])
        self.assertIn("针对上一版的具体反馈", model.system_prompts[2])
        self.assertEqual([(item.role, item.resource) for item in turn.skill_events if item.stage == "reference"],
                         [("DS", "references/code-repair.md")])

    def test_keeps_verified_result_if_interpreter_suggests_a_regression(self):
        broken = _GOOD_CODE.replace('row["day"]', 'row["date"]')
        model = ScriptedAnalysis(_GOOD_CODE, broken,
                                 review={"decision": "code_update", "feedback": "Check date pairing once more."})
        with closing(Store(":memory:")) as store:
            _load_days(store)
            turn = Orchestrator(store, ds_model=model).run(_QUESTION, "demo", _AS_OF)
        result, = turn.sandbox_analyses
        self.assertEqual([attempt.status for attempt in result.attempts],
                         ["code_update", "execution_error", "generation_error", "generation_error"])
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.result["difference_hours"], 3)

    def test_compares_deep_sleep_using_the_saved_steps_goal(self):
        model = ScriptedAnalysis(_GOOD_CODE)
        with closing(Store(":memory:")) as store:
            _load_days(store)
            agent = Orchestrator(store, ds_model=model)
            agent.run("把步数目标设为每天7000步", "demo", _AS_OF)
            turn = agent.run("过去6天，步数达标的日子当晚的深睡时长更长吗？", "demo", _AS_OF)
        analysis, = turn.sandbox_analyses
        self.assertEqual(analysis.plan.sleep_metric, "deep_sleep_hours")
        self.assertEqual(analysis.result["step_cutoff"], 7000)
        self.assertEqual(analysis.result["high_days"], 3)
        self.assertAlmostEqual(analysis.result["difference_hours"], 4 / 3)

    def test_file_access_is_rejected_as_invalid_generated_code(self):
        execution = PythonSandbox().run('def analysis(rows, plan):\n    return {"number": open("notes.txt")}', [], {})
        self.assertEqual(execution.status, "execution_error")
        self.assertIn("provided numeric helpers", execution.feedback)

    def test_runtime_feedback_omits_exception_values_from_rows(self):
        code = 'def analysis(rows, plan):\n    return {"number": plan[rows[0]["steps"]]}'
        execution = PythonSandbox().run(code, [{"steps": 1234567}], {})
        self.assertEqual(execution.status, "execution_error")
        self.assertEqual(execution.feedback, "KeyError in generated analysis at line 2.")


class SandboxRoutingTest(unittest.TestCase):
    def test_negated_joint_request_never_starts_sandbox(self):
        model = ScriptedAnalysis()
        with closing(Store(":memory:")) as store:
            turn = Orchestrator(store, ds_model=model).run("不分析步数和当晚深睡的关系。只查本周步数。", "demo", _AS_OF)
        self.assertEqual([step.operation for step in turn.trace], ["measure"])
        self.assertFalse(turn.sandbox_analyses)
        self.assertFalse(model.requests)
