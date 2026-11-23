import unittest
from contextlib import closing
from datetime import date

from health_agent.domain import Metric
from health_agent.evidence import EvidenceResearch, EvidenceScope
from health_agent.evidence_sources import EvidenceDocument, EvidenceSources, SourceUnavailable
from health_agent.orchestrator import Orchestrator
from health_agent.store import Store


class LiteratureSource:
    web_key = ""

    def __init__(self):
        self.queries = []

    def pubmed(self, term):
        self.queries.append(term)
        return (EvidenceDocument("PubMed", "Activity and sleep in older adults",
                                 "https://pubmed.ncbi.nlm.nih.gov/123/",
                                 "The six-week intervention measured physical activity and reported sleep duration."),)


class QuoteModel:
    def __init__(self):
        self.inputs = []

    def structured(self, system, payload, schema):
        self.inputs.append(payload)
        if "options" in payload:
            return {"id": payload["options"][0]["name"]}
        return {"quotes": [{"id": 0, "quote": "Walking increased deep sleep for everyone."}]}


class EvidenceTest(unittest.TestCase):
    def test_examination_rejects_invented_quotes_and_retries_an_indirect_result(self):
        source = LiteratureSource()
        model = QuoteModel()
        scope = EvidenceScope.from_question(Metric.SLEEP, "当晚深睡和步数", joint=True)
        report = EvidenceResearch(source, model).run(scope, "Private individual watch reading")
        self.assertEqual([item.round for item in report.investigations], [1, 2])
        self.assertNotEqual(*source.queries)
        finding, = report.findings
        self.assertEqual(finding.fit, "context")
        self.assertEqual(finding.url, "https://pubmed.ncbi.nlm.nih.gov/123/")
        self.assertIn("睡眠结局口径未确认", finding.scope_notes)
        self.assertNotIn("for everyone", report.describe())
        self.assertNotIn("Private individual watch reading", str(model.inputs) + str(source.queries))

    def test_research_question_routes_to_de_without_local_analysis(self):
        research = EvidenceResearch(LiteratureSource())
        with closing(Store(":memory:")) as store:
            turn = Orchestrator(store, evidence=research).run("查研究：步数与当晚睡眠的关系", "evidence", date(2026, 11, 23))
        self.assertEqual([event.operation for event in turn.trace], ["retrieve_evidence"])
        self.assertFalse(turn.sandbox_analyses)
        self.assertEqual(turn.evidence_runs[0].personal_observation, "")

    def test_public_web_search_falls_back_to_medlineplus(self):
        class WebSources:
            web_key = "test"

            def tavily(self, term):
                raise SourceUnavailable("HTTP 403")

            def medlineplus(self, term):
                return (EvidenceDocument("MedlinePlus.gov", "Healthy Sleep", "https://medlineplus.gov/healthysleep.html",
                                         "Sleep duration depends on age."),)

        report = EvidenceResearch(WebSources()).run(EvidenceScope.from_question(Metric.SLEEP, "健康的睡眠时长"))
        self.assertEqual([(item.tool, item.status) for item in report.investigations],
                         [("tavily", "unavailable"), ("medlineplus", "ok")])
        self.assertEqual(report.findings[0].source, "MedlinePlus.gov")

    def test_data_commons_preserves_the_observation_and_its_provenance(self):
        class Sources(EvidenceSources):
            def _json(self, url, parameters, *, body, auth):
                entity, = body["entity"]["dcids"]
                variable, = body["variable"]["dcids"]
                return {"byVariable": {variable: {"byEntity": {entity: {"orderedFacets": [
                    {"facetId": "42", "observations": [{"date": "2022", "value": 31.8}]}
                ]}}}}, "facets": {"42": {"provenanceUrl": "https://www.cdc.gov/places/",
                                         "measurementMethod": "CrudePrevalence"}}}

        research = EvidenceResearch(Sources(population_key="test"))
        with closing(Store(":memory:")) as store:
            turn = Orchestrator(store, evidence=research).run(
                "美国圣克拉拉县成年人睡眠少于7小时的比例是多少？", "demo", date(2026, 11, 23))
        report, = turn.evidence_runs
        finding = report.findings[0]
        stat = finding.population_statistic
        self.assertEqual((stat.place, stat.year, stat.percent), ("美国圣克拉拉县", "2022", 31.8))
        self.assertEqual(stat.provenance, "https://www.cdc.gov/places/")
        self.assertEqual(finding.fit, "population")
        self.assertEqual([item.tool for item in report.investigations], ["data_commons"])
