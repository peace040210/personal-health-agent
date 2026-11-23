import asyncio
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path

from health_agent.model import ToolCall
from health_agent.orchestrator import Orchestrator
from health_agent.sdk import HealthTools
from health_agent.store import Store

try:
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters
except ImportError:
    Client = None


ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 11, 22)
ARGUMENTS = {"metric": "sleep_hours", "windows": [{"start": "2026-11-16", "end": "2026-11-22"}]}


class ToolCallingModel:
    def __init__(self, replacement=None):
        self.replacement = replacement
        self.requests = []

    def tool_calls(self, system, payload, tools):
        self.requests.append((payload, tools))
        name = tools[0]["function"]["name"]
        return (self.replacement or ToolCall(name, payload["approved_arguments"]),)


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.tools = HealthTools(self.store)
        imported = self.tools.call("import_daily_records", {"path": str(ROOT / "examples/week_2.csv")})
        self.assertEqual(imported, {"days": 7})

    def test_sdk_returns_json_statistics_and_goal_review(self):
        Orchestrator(self.store).run("把我的睡眠目标设为每天 8 小时", "sdk", TODAY)
        goal = self.tools.call("get_goal", {"metric": "sleep_hours"})
        self.assertEqual(goal, {"metric": "sleep_hours", "target": 8.0, "unit": "小时"})
        review = self.tools.call("review_goal", ARGUMENTS)
        self.assertEqual(review["stats"][0]["window"], ARGUMENTS["windows"][0])
        self.assertEqual(review["stats"][0]["samples"], 6)
        self.assertEqual(review["stats"][0]["met"], 4)
        self.assertIn("4 天达标", review["reply"])

    def test_sdk_rejects_out_of_order_windows_and_extra_fields(self):
        with self.assertRaisesRegex(ValueError, "end date"):
            self.tools.call("query_metric", {"metric": "steps", "windows": [
                {"start": "2026-11-22", "end": "2026-11-16"}
            ]})
        with self.assertRaises(ValueError):
            self.tools.call("review_goal", {**ARGUMENTS, "goal": 2})

    def test_chat_executes_an_approved_model_call(self):
        model = ToolCallingModel()
        turn = Orchestrator(self.store, tool_model=model).run("查本周的睡眠", "model", TODAY)
        call, = turn.tool_calls
        self.assertEqual((call.name, call.source, call.arguments), ("query_metric", "model", ARGUMENTS))
        self.assertIn("记录 6/7 天", turn.reply)
        payload, tools = model.requests[0]
        self.assertEqual(list(payload), ["task", "approved_arguments"])
        self.assertEqual([tool["function"]["name"] for tool in tools], ["query_metric"])

    def test_chat_falls_back_when_the_model_changes_scope_or_requests_a_write(self):
        invalid_calls = (
            ToolCall("query_metric", {**ARGUMENTS, "metric": "steps"}),
            ToolCall("import_daily_records", {"path": "ignored.csv"}),
        )
        for invalid_call in invalid_calls:
            with self.subTest(call=invalid_call):
                model = ToolCallingModel(invalid_call)
                turn = Orchestrator(self.store, tool_model=model).run("查本周的睡眠", "invalid", TODAY)
                call, = turn.tool_calls
                self.assertEqual((call.name, call.source, call.arguments),
                                 ("query_metric", "workflow_fallback", ARGUMENTS))
                self.assertEqual(turn.trace[0].metric, "sleep_hours")


@unittest.skipUnless(Client, "requires the optional MCP SDK")
class McpToolsTest(unittest.TestCase):
    def test_tools_are_listed_and_executed_over_stdio(self):
        async def call_tools(database: Path):
            process = StdioServerParameters(command=sys.executable, args=[
                "-m", "health_agent.mcp_server", "--db", str(database)
            ], cwd=ROOT)
            async with Client(process, read_timeout_seconds=10) as client:
                listed = (await client.list_tools()).tools
                self.assertEqual({tool.name for tool in listed}, {"query_metric", "get_goal", "review_goal"})
                self.assertTrue(all(tool.annotations.read_only_hint for tool in listed))
                result = await client.call_tool("query_metric", ARGUMENTS)
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content["stats"][0]["samples"], 6)
                result = await client.call_tool("import_daily_records", {"path": "ignored.csv"})
                self.assertTrue(result.is_error)

        with tempfile.TemporaryDirectory(prefix="pha mcp ") as directory:
            database = Path(directory) / "test.sqlite3"
            with closing(Store(database)) as store:
                store.import_csv(ROOT / "examples/week_2.csv")
            original = database.read_bytes()
            asyncio.run(call_tools(database))
            self.assertEqual(database.read_bytes(), original)
