import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

if TestClient is not None:
    from health_agent.web import WebConfig, create_app


@unittest.skipUnless(TestClient is not None, "requires the web and test extras")
class QibuAgentWebTest(unittest.TestCase):
    def chat(self, client, session, message):
        response = client.post("/api/qibu/chat", json={"session_id": session, "message": message})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def action(self, client, session, draft, operation, day=None):
        return client.post("/api/qibu/actions", json={"session_id": session, "action_id": draft["id"],
                                                     "operation": operation, "day": day})

    def snapshot(self, client, session):
        return client.get("/api/qibu/session", params={"session_id": session}).json()

    def test_reads_the_specific_night_and_asks_for_a_missing_date(self):
        sid = str(uuid4())
        with TestClient(create_app()) as client:
            self.assertEqual(client.get("/qibu/").status_code, 200)
            night = self.chat(client, sid, "我昨天晚上睡了几个小时？")
            self.assertEqual(night["title"], "当晚睡眠 6 小时 35 分")
            self.assertIn("2024-12-14", night["lines"][0])
            self.assertIn("23:07", night["lines"][1])
            self.assertEqual(self.chat(client, sid, "昨天晚上的数据")["title"], night["title"])
            self.assertEqual(self.chat(client, sid, "那深睡呢")["title"], "当晚深睡 1 小时 13 分")
            self.assertEqual(self.chat(client, sid, "2026-11-22 晚上睡了多久")["title"], "这个日期范围没有相关记录")
            sid = str(uuid4())
            self.assertIn("哪一天", self.chat(client, sid, "查查睡眠")["title"])
            self.assertEqual(self.chat(client, sid, "昨天")["title"], night["title"])
            self.assertEqual(self.chat(client, sid, "过去7天我平均走了多少步")["title"], "步数日均 8,865 步")

    def test_model_interpretation_dispatches_to_verified_evidence(self):
        class Model:
            def structured(self, system, payload, schema, *, max_tokens=512):
                if "意图与参数提取器" in system:
                    return {"intent": "knowledge", "area": "sleep_stages"}
                return None

        with TestClient(create_app(WebConfig(model=Model()))) as client:
            reply = self.chat(client, str(uuid4()), "睡到哪种阶段才算沉")
        self.assertEqual(reply["understanding"], "model")
        self.assertEqual(reply["agents"], ["DE"])
        self.assertIn("nhlbi.nih.gov", reply["sources"][0]["url"])

    def test_revised_action_requires_confirmation_and_persists(self):
        sid, other = str(uuid4()), str(uuid4())
        with TemporaryDirectory() as temp:
            config = WebConfig(qibu_dir=Path(temp))
            with TestClient(create_app(config)) as client:
                draft = self.chat(client, sid, "我今天需要午休十分钟")["draft"]
                self.assertEqual(draft["minutes"], 10)
                updated = self.chat(client, sid, "改成十五分钟")["draft"]
                self.assertEqual(updated["minutes"], 15)
                self.assertEqual(self.snapshot(client, sid)["actions"], [])
                self.assertEqual(self.action(client, sid, draft, "confirm").status_code, 409)
                self.assertEqual(self.action(client, other, updated, "confirm").status_code, 404)
                self.assertEqual(self.action(client, sid, updated, "confirm").status_code, 200)
                self.assertEqual(self.action(client, sid, updated, "confirm").status_code, 200)
                self.assertEqual(len(self.snapshot(client, sid)["actions"]), 1)
                self.assertEqual(self.action(client, sid, updated, "complete", updated["day"]).status_code, 200)
                device = dict(client.cookies)
            with TestClient(create_app(config)) as restarted:
                restarted.cookies.update(device)
                saved = self.snapshot(restarted, sid)["actions"]
                self.assertEqual(saved[0]["checked"], [updated["day"]])
                self.assertEqual(self.snapshot(restarted, other)["actions"], [])

    def test_clarifies_duration_before_proposing_a_weekly_action(self):
        with TestClient(create_app()) as client:
            sid = str(uuid4())
            reply = self.chat(client, sid, "帮我安排一周每天散步")
            self.assertIn("几分钟", reply["title"])
            self.assertIsNone(reply["draft"])
            draft = self.chat(client, sid, "十分钟")["draft"]
            self.assertEqual((draft["days"], draft["minutes"]), (7, 10))

    def test_an_action_label_from_the_model_cannot_make_a_past_report_a_draft(self):
        class Model:
            def structured(self, system, payload, schema, *, max_tokens=512):
                return {"intent": "action", "action": "nap", "minutes": 10}

        with TestClient(create_app(WebConfig(model=Model()))) as client:
            sid = str(uuid4())
            self.assertIsNone(self.chat(client, sid, "我今天已经午休了十分钟")["draft"])
            self.assertEqual(self.snapshot(client, sid)["actions"], [])

    def test_one_time_correction_overrides_the_previous_weekly_plan(self):
        class Model:
            def structured(self, system, payload, schema, *, max_tokens=512):
                return {"intent": "action", "cadence": "week"}

        with TestClient(create_app(WebConfig(model=Model()))) as client:
            sid = self.new_conversation(client)
            old = self.chat(client, sid, "帮我排个一周的散步计划，每天十五分钟")["draft"]
            revised = self.chat(client, sid, "今天就先午休十分钟")
            self.assertEqual((old["days"], revised["draft"]["days"]), (7, 1))
            self.assertEqual(self.action(client, sid, old, "confirm").status_code, 409)
            closed = self.chat(client, sid, "还是先不安排了")
            self.assertIsNone(closed["pending_draft_id"])
            self.assertEqual(self.action(client, sid, revised["draft"], "confirm").status_code, 409)
            self.assertEqual(self.snapshot(client, sid)["actions"], [])

    def test_chat_retry_resumes_the_committed_result_without_new_action_or_history(self):
        with TestClient(create_app()) as client:
            sid = self.new_conversation(client)
            request = {"session_id": sid, "turn_id": str(uuid4()), "message": "今天就先午休十分钟"}
            first = client.post("/api/qibu/chat", json=request).json()
            self.assertEqual(self.action(client, sid, first["draft"], "confirm").status_code, 200)
            replay = client.post("/api/qibu/chat", json=request).json()
            self.assertEqual(replay["draft"], first["draft"])
            self.assertEqual(replay["draft_status"], "active")
            self.assertIsNone(replay["pending_draft_id"])
            state = self.snapshot(client, sid)
            self.assertEqual((len(state["history"]), len(state["actions"])), (1, 1))
            changed = client.post("/api/qibu/chat", json={**request, "message": "今天午休二十分钟"})
            self.assertEqual(changed.status_code, 409)

    def test_legacy_conversation_history_remains_available_after_retry_support_is_added(self):
        sid = str(uuid4())
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            with closing(sqlite3.connect(directory / f"{sid}.sqlite3")) as db, db:
                db.execute("CREATE TABLE qibu_history (id INTEGER PRIMARY KEY, message TEXT NOT NULL, reply TEXT NOT NULL)")
                db.execute("INSERT INTO qibu_history VALUES (1, ?, ?)", ("谢谢", json.dumps({"title": "好的。"})))
            with TestClient(create_app(WebConfig(qibu_dir=directory))) as client:
                request = {"session_id": sid, "turn_id": str(uuid4()), "message": "先这样"}
                self.assertEqual(client.post("/api/qibu/chat", json=request).status_code, 200)
                history = self.snapshot(client, sid)["history"]
                self.assertEqual([turn["message"] for turn in history], ["谢谢", "先这样"])

    def test_provider_failure_uses_rules_and_keeps_the_selected_date(self):
        class UnavailableModel:
            def structured(self, system, payload, schema, *, max_tokens=512):
                return None

        with TestClient(create_app(WebConfig(model=UnavailableModel()))) as client:
            sid = self.new_conversation(client)
            missing = self.chat(client, sid, "帮我看看 2026-11-20 那晚总共睡了多久")
            self.assertEqual((missing["title"], missing["understanding"]), ("这个日期范围没有相关记录", "fallback"))
            follow_up = self.chat(client, sid, "那深睡有吗")
            self.assertIn("2026-11-20", follow_up["lines"][0])
            self.assertIsNone(follow_up["draft"])
            self.assertEqual(self.snapshot(client, sid)["actions"], [])

    def new_conversation(self, client):
        response = client.post("/api/qibu/conversations")
        self.assertEqual(response.status_code, 200)
        return response.json()["id"]

    def memory(self, client, session, operation, draft=None):
        payload = {"session_id": session, "operation": operation}
        if draft:
            payload["draft_id"] = draft["id"]
        return client.post("/api/qibu/memory", json=payload)

    def test_history_and_absolute_date_resume_after_restart_and_are_owner_scoped(self):
        sid = str(uuid4())
        with TemporaryDirectory() as temp:
            config = WebConfig(qibu_dir=Path(temp))
            with TestClient(create_app(config)) as client:
                night = self.chat(client, sid, "昨晚睡了多久")
                self.assertIn("2024-12-14", night["lines"][0])
                draft = self.chat(client, sid, "我工作日午休只有十分钟")["memory_draft"]
                self.assertEqual(self.memory(client, sid, "confirm", draft).status_code, 200)
                device = dict(client.cookies)
            with TestClient(create_app(config)) as restarted:
                restarted.cookies.update(device)
                state = self.snapshot(restarted, sid)
                self.assertEqual(state["memory"]["minutes"], 10)
                history = state["history"]
                self.assertEqual(history[0]["message"], "昨晚睡了多久")
                self.assertEqual(history[0]["reply"]["title"], night["title"])
                answer = self.chat(restarted, sid, "那深睡呢")
                self.assertEqual(answer["title"], "当晚深睡 1 小时 13 分")
                self.assertEqual(restarted.get("/api/qibu/conversations").json()[0]["id"], sid)
            with TestClient(create_app(config)) as stranger:
                response = stranger.get("/api/qibu/session", params={"session_id": sid})
                self.assertEqual(response.status_code, 404)
                self.assertEqual(stranger.get("/api/qibu/conversations").json(), [])

    def test_only_confirmed_recurring_limit_is_reused_across_conversations(self):
        with patch("health_agent.web.datetime", wraps=datetime) as clock, TestClient(create_app()) as client:
            clock.now.return_value = datetime(2026, 11, 23, 12, 30)
            first = self.new_conversation(client)
            today = self.chat(client, first, "我今天需要午休十分钟")
            self.assertIsNone(today["memory_draft"])
            self.assertEqual(self.action(client, first, today["draft"], "confirm").status_code, 200)
            self.assertIsNone(self.snapshot(client, first)["memory"])
            recurring = self.chat(client, first, "我工作日午休只有十分钟")
            draft = recurring["memory_draft"]
            self.assertIsNone(recurring["draft"])
            self.assertIsNone(self.snapshot(client, first)["memory"])
            other = self.new_conversation(client)
            request = "按我平时的时间安排午休"
            self.assertIsNone(self.chat(client, other, request)["draft"])
            self.assertEqual(self.memory(client, other, "confirm", draft).status_code, 404)
            self.assertEqual(self.memory(client, first, "confirm", draft).json()["memory"]["minutes"], 10)
            saved = self.chat(client, other, request)
            self.assertEqual(saved["draft"]["minutes"], 10)
            self.assertTrue(any("已使用" in line for line in saved["lines"]))
            self.assertEqual(saved["draft"]["days"], 1)
            self.assertEqual(self.chat(client, other, "今天午休十五分钟")["draft"]["minutes"], 15)
            self.assertEqual(self.snapshot(client, other)["memory"]["minutes"], 10)
            self.assertIsNone(self.chat(client, other, "按我平时的时间安排散步")["draft"])
            self.assertIsNone(self.chat(client, other, "2026-11-24 安排中午午休")["draft"])
            clock.now.return_value = datetime(2026, 11, 28, 12, 30)
            self.assertIsNone(self.chat(client, other, request)["draft"])

    def test_confirmation_update_and_forgetting_do_not_reopen_stale_proposals(self):
        with TestClient(create_app()) as client:
            s = self.new_conversation(client)
            old = self.chat(client, s, "我工作日午休只能十分钟")["memory_draft"]
            new = self.chat(client, s, "我工作日午休现在只有十五分钟")["memory_draft"]
            self.assertEqual(self.memory(client, s, "confirm", old).status_code, 409)
            self.assertEqual(self.memory(client, s, "confirm", new).json()["memory"]["minutes"], 15)
            self.assertEqual(self.memory(client, s, "confirm", new).json()["memory"]["minutes"], 15)
            next_draft = self.chat(client, s, "我工作日午休只有二十分钟")["memory_draft"]
            self.assertEqual(self.memory(client, s, "decline", next_draft).json()["memory"]["minutes"], 15)
            self.assertIsNone(self.memory(client, s, "forget").json()["memory"])
            self.assertEqual(self.memory(client, s, "confirm", new).status_code, 409)
            self.assertEqual(self.snapshot(client, s)["history"][1]["reply"]["memory_status"], "forgotten")
