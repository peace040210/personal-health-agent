import json
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from health_agent.domain import Metric
from health_agent.evidence import EvidenceResearch, EvidenceScope
from health_agent.evidence_sources import EvidenceDocument
from health_agent.knowledge import LOCAL_KNOWLEDGE, KnowledgeBase
from health_agent.orchestrator import Orchestrator
from health_agent.store import Store


URL = "https://www.nhlbi.nih.gov/health/sleep/stages-of-sleep"


class Embeddings:
    unavailable = False

    def embed(self, model, passages):
        if self.unavailable:
            raise ValueError("service down")
        return [[1.0, 0.0] for _ in passages]


class KnowledgeTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "notes.jsonl"
        self.path = self.root / "knowledge.sqlite3"

    def write(self, text):
        item = {"id": "sleep-stages", "title": "睡眠分期说明", "url": URL, "text": text}
        self.source.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")

    def test_reimport_updates_full_text_without_duplicates(self):
        knowledge = KnowledgeBase(self.path)
        self.write("深睡时长是这一睡眠阶段的时长。oldlabel")
        self.assertEqual(knowledge.import_jsonl(self.source), (1, 1))
        self.assertEqual(knowledge.import_jsonl(self.source), (0, 0))
        self.assertEqual(knowledge.search("oldlabel").documents[0].url, URL)
        self.write("深睡时长只统计深睡阶段。newlabel")
        self.assertEqual(knowledge.import_jsonl(self.source), (1, 1))
        self.assertFalse(knowledge.search("oldlabel").documents)
        self.assertEqual(len(knowledge.search("深睡").documents), 1)

    def test_vector_retrieval_and_bm25_fallback(self):
        service = Embeddings()
        knowledge = KnowledgeBase(self.path, embeddings=service)
        self.write("深睡又称慢波睡眠。")
        knowledge.import_jsonl(self.source, embedding_model="fixture")
        result = knowledge.search("n3")
        self.assertEqual(result.mode, "hybrid")
        self.assertEqual(result.documents[0].url, URL)
        service.unavailable = True
        result = knowledge.search("慢波睡眠")
        self.assertEqual(result.mode, "bm25_fallback")
        self.assertEqual(result.documents[0].url, URL)

    def test_de_injects_local_passage_and_checks_model_excerpt(self):
        sentence = "深睡时长只统计慢波睡眠这一阶段。"

        class Model:
            quote = sentence
            prompt = None

            def structured(self, system, payload, schema):
                self.prompt = payload
                return {"quotes": [{"id": 0, "quote": self.quote}]}

        self.write(sentence + "总睡眠时长还涵盖其他阶段。")
        knowledge = KnowledgeBase(self.path)
        knowledge.import_jsonl(self.source)
        model = Model()
        evidence = EvidenceResearch(object(), model, knowledge)
        with closing(Store(":memory:")) as store:
            turn = Orchestrator(store, evidence=evidence).run("什么是深睡？", "rag", date(2026, 11, 23))
        run, = turn.evidence_runs
        finding, = run.findings
        self.assertEqual([item.tool for item in run.investigations], ["local_kb"])
        self.assertIn(sentence, model.prompt["sources"][0]["text"])
        self.assertEqual((finding.source, finding.url, finding.quote), (LOCAL_KNOWLEDGE, URL, sentence))
        self.assertTrue(run.used_model)
        model.quote = "深睡能治愈所有疾病。"
        answer = evidence.run(EvidenceScope.from_question(Metric.SLEEP, "什么是深睡"))
        self.assertNotIn(model.quote, answer.describe())

    def test_local_background_does_not_replace_joint_research(self):
        class Literature:
            web_key = ""

            def pubmed(self, term):
                return (EvidenceDocument("PubMed", "Daily steps and sleep duration", "https://pubmed.ncbi.nlm.nih.gov/123/",
                                         "Measurements included daily steps and total sleep time the following night."),)

        self.write("每日步数和记录日按定义配对。睡眠时长分组的差异需要外部研究解释。")
        knowledge = KnowledgeBase(self.path)
        knowledge.import_jsonl(self.source)
        scope = EvidenceScope.from_question(Metric.SLEEP, "步数与当晚睡眠是否有关", joint=True)
        run = EvidenceResearch(Literature(), knowledge=knowledge).run(scope)
        self.assertEqual([item.tool for item in run.investigations], ["local_kb", "pubmed"])
        self.assertEqual({(item.source, item.fit) for item in run.findings},
                         {(LOCAL_KNOWLEDGE, "context"), ("PubMed", "direct")})
