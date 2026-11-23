import json
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from health_agent.coaching import CoachingLoop
from health_agent.domain import Metric
from health_agent.evidence import EvidenceResearch, EvidenceScope
from health_agent.evidence_sources import EvidenceDocument
from health_agent.orchestrator import Orchestrator
from health_agent.skill_evolution import evaluate_de, evaluation_summary, evolve_de
from health_agent.skills import SkillRegistry
from health_agent.store import Store


class Proposer:
    def __init__(self, *proposals):
        self.payloads = []
        self.proposals = proposals

    def structured(self, system, payload, schema, *, max_tokens):
        self.payloads.append(payload)
        return {"candidates": list(self.proposals)}


def candidate(order):
    return {"joint_search_order": order, "guidance": "按选定策略匹配步数与睡眠。",
            "rationale": "比较遗漏的时序证据和已有的精确命中。"}


class SkillTest(unittest.TestCase):
    def test_only_metadata_is_available_to_the_planner_and_other_roles_stay_lazy(self):
        class Planner:
            def structured(self, system, payload, schema):
                self.payload = payload
                return None

        planner = Planner()
        with TemporaryDirectory() as folder, closing(Store(":memory:")) as store:
            # An empty body in an unused role is harmless until that role is activated.
            de = Path(folder) / "v2" / "domain-expert"
            de.mkdir(parents=True)
            (de / "SKILL.md").write_text("---\nname: domain-expert\ndescription: Test catalog\n---\n", encoding="utf-8")
            registry = SkillRegistry(Path(folder), {"DE": "v2"})
            turn = Orchestrator(store, workflow_model=planner, skills=registry).run(
                "过去七天我走了多少步？", "skills", date(2026, 11, 23))
            with self.assertRaisesRegex(ValueError, "正文为空"):
                registry.start().activate("DE")
        self.assertEqual([(item["role"], item["version"]) for item in planner.payload["skill_catalog"]],
                         [("DS", "v1"), ("DE", "v2"), ("HC", "v1")])
        self.assertTrue(all(set(item) == {"role", "name", "description", "version"}
                            for item in planner.payload["skill_catalog"]))
        self.assertEqual([item.role for item in turn.skill_events if item.stage == "activate"], ["DS"])
        self.assertFalse([item for item in turn.skill_events if item.stage == "reference"])

    def test_examination_and_coaching_read_own_references_only_when_needed(self):
        class Sources:
            web_key = ""

            def __init__(self, matches):
                self.matches = matches

            def medlineplus(self, query):
                return self.matches

            def pubmed(self, query):
                return ()

        class Model:
            def structured(self, system, payload, schema):
                if "options" in payload:
                    return {"id": payload["options"][0]["name"]}
                if "sources" in payload:
                    self.examination = system
                    return {"quotes": [{"id": 0, "quote": "invented content"}]}
                self.coach_prompt = system
                self.coach_payload = payload
                return {"choice": 0}

        model = Model()
        session = SkillRegistry().start()
        de = session.activate("DE")
        scope = EvidenceScope.from_question(Metric.SLEEP, "睡眠时长是什么意思")
        self.assertFalse(EvidenceResearch(Sources(()), model).run(scope, skill=de).findings)
        self.assertFalse([item for item in session.events if item.stage == "reference"])
        doc = EvidenceDocument("MedlinePlus.gov", "Sleep duration", "https://example.invalid/sleep",
                               "Sleep duration measures the amount of time spent asleep.")
        findings = EvidenceResearch(Sources((doc,)), model).run(scope, skill=de).findings
        self.assertEqual(findings[0].quote, doc.text)
        self.assertIn("实际测量的是每日步数", model.examination)
        hc = session.activate("HC")
        draft = CoachingLoop(model).draft("加班没时间，我想多走点", Metric.STEPS, None, "一个已核对的统计",
                                          skill=hc, domain_context={"external_findings": [doc.title]})
        self.assertEqual(draft.action, "propose")
        self.assertIn("门槛最低", model.coach_prompt)
        self.assertEqual(model.coach_payload["personal_observation"], "一个已核对的统计")
        self.assertEqual(model.coach_payload["external_evidence_context"]["external_findings"], [doc.title])
        self.assertEqual([item.resource for item in session.events if item.stage == "reference"],
                         ["references/source-fit.md", "references/adjustment.md"])
        with self.assertRaises(ValueError):
            hc.reference("../private.md")


class EvolutionTest(unittest.TestCase):
    def test_evaluation_gates_candidate_and_reuses_persisted_experience(self):
        registry = SkillRegistry()
        proposer = Proposer(candidate("metrics-first"), candidate("retry-without-timing"))
        with TemporaryDirectory() as folder:
            root = Path(folder)
            report = evolve_de(proposer, registry, root, generations=1)
            version = report["released_version"]
            self.assertEqual(report["status"], "released")
            self.assertEqual(report["rounds"][0]["candidates"][0]["status"], "rejected")
            self.assertEqual(report["rounds"][0]["candidates"][0]["val_regressions"], 5)
            self.assertEqual(report["baseline"]["test"]["passed"], 10)
            self.assertEqual(report["final"]["test"]["passed"], 15)
            self.assertEqual(report["final"]["test"]["guard_failures"], 0)
            self.assertEqual(proposer.payloads[0]["experience"], [])
            payload = json.dumps(proposer.payloads, ensure_ascii=False)
            for hidden in ("n3-only-in-abstract", "duration-following-night", "白天步数和同一晚深度睡眠"):
                self.assertNotIn(hidden, payload)
            pinned = SkillRegistry(root, {"DE": version})
            self.assertEqual(evaluation_summary(evaluate_de(pinned, "test"))["passed"], 15)
            self.assertEqual([item.version for item in pinned.metadata], ["v1", version, "v1"])
            self.assertEqual(registry.start().activate("DE").evidence_policy().joint_search_order, "legacy")
            another = Proposer(candidate("metrics-first"))
            followup = evolve_de(another, pinned, root, generations=1)
            self.assertEqual(followup["status"], "no_improvement")
            self.assertEqual(followup["used_prior_lessons"], 1)
            self.assertEqual(another.payloads[0]["experience"][0]["version"], version)
            self.assertIsNone(followup["released_version"])

    def test_invalid_mutation_creates_no_release(self):
        proposer = Proposer(candidate("retry-without-timing") | {"tools": ["shell"]})
        with TemporaryDirectory() as folder:
            root = Path(folder)
            report = evolve_de(proposer, SkillRegistry(), root, generations=1)
            self.assertEqual(report["status"], "no_valid_proposals")
            self.assertEqual([path.name for path in root.iterdir()], ["runs"])
            self.assertEqual(report["rounds"][0]["candidates"][0]["status"], "invalid")
