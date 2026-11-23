import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

if TestClient is not None:
    from health_agent.web import WebConfig, create_app


@unittest.skipUnless(TestClient is not None, "requires the web and test extras")
class WebDemoTest(unittest.TestCase):
    def test_serves_frontend_and_public_replays(self):
        with TemporaryDirectory() as directory:
            frontend = Path(directory)
            (frontend / "index.html").write_text("<h1>Demo</h1>", encoding="utf-8")
            with TestClient(create_app(WebConfig(frontend=frontend))) as client:
                self.assertIn("<h1>Demo</h1>", client.get("/").text)
                sample = client.get("/api/demo").json()
                report = client.get("/api/evolution").json()
        self.assertEqual(sample["dataset"]["days"], 365)
        self.assertFalse(sample["capabilities"]["joint_live"])
        replay = sample["replay"]
        self.assertEqual(replay["mode"], "replay")
        self.assertEqual([item["agent"] for item in replay["details"]["trace"]], ["DS", "DE", "HC"])
        self.assertEqual(replay["details"]["sandbox_analyses"][0]["result"]["paired_days"], 30)
        self.assertNotIn("code", replay["details"]["sandbox_analyses"][0]["attempts"][0])
        self.assertEqual(report["final"]["test"]["passed"], 15)
        self.assertTrue(any(item["val_regressions"] > 0 for item in report["candidates"]))

    def test_streams_actual_ds_events_and_finishes_with_the_turn(self):
        with TestClient(create_app()) as client:
            response = client.post("/api/chat/stream", json={
                "session_id": str(uuid4()), "message": "过去7天我一共走了多少步？",
            })
        self.assertEqual(response.headers["content-type"], "text/event-stream; charset=utf-8")
        records = [block.splitlines() for block in response.text.strip().split("\n\n")]
        kinds = [header.removeprefix("event: ") for header, _ in records]
        values = [json.loads(data.removeprefix("data: ")) for _, data in records]
        self.assertEqual(kinds, ["progress", "progress", "progress", "done"])
        self.assertEqual([event["phase"] for event in values[:-1]], ["planned", "started", "completed"])
        result = values[-1]
        self.assertEqual(result["mode"], "live")
        self.assertIn("62,058 步", result["reply"])
        self.assertEqual([item["role"] for item in result["details"]["skill_events"] if item["stage"] == "activate"],
                         ["DS"])

    def test_isolates_goals_and_can_answer_from_local_evidence(self):
        with TestClient(create_app()) as client:
            first, second = str(uuid4()), str(uuid4())

            def chat(session, text):
                response = client.post("/api/chat", json={"session_id": session, "message": text})
                self.assertEqual(response.status_code, 200)
                return response.json()

            self.assertIn("8000", chat(first, "把步数目标设为每天8000步")["reply"])
            self.assertIn("8000", chat(first, "复盘过去7天我的步数目标")["reply"])
            self.assertIn("还没有保存", chat(second, "复盘过去7天我的步数目标")["reply"])
            result = chat(second, "什么是深睡？")
            self.assertEqual([row["tool"] for row in result["details"]["evidence_runs"][0]["investigations"]],
                             ["local_kb"])
            self.assertEqual(client.post("/api/chat", json={"session_id": first, "message": "  "}).status_code, 422)
