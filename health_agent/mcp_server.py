"""Expose the registered read-only tools to local MCP clients over stdio."""

import argparse
import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .model import DeepSeekWorkflow, Ollama, WorkflowModel
from .sdk import HealthTools
from .store import Store


def create_server(database: Path, ds_model: WorkflowModel | None = None):
    from mcp import types
    from mcp.server import Server

    with closing(Store(database, read_only=True)) as store:
        specs = {spec.name: spec for spec in HealthTools(store, ds_model=ds_model).specs(read_only=True)}

    async def list_tools(_context, _params):
        return types.ListToolsResult(tools=[
            types.Tool(name=spec.name, description=spec.description, input_schema=spec.parameters,
                       annotations=types.ToolAnnotations(read_only_hint=True))
            for spec in specs.values()
        ])

    def invoke(name: str, arguments: dict) -> dict:
        with closing(Store(database, read_only=True)) as store:
            return HealthTools(store, ds_model=ds_model).call(name, arguments)

    async def call_tool(_context, params):
        if params.name not in specs:
            return types.CallToolResult(is_error=True, content=[
                types.TextContent(type="text", text="This tool is not available on the read-only server.")
            ])
        try:
            result = await asyncio.to_thread(invoke, params.name, params.arguments or {})
        except (ValueError, OSError, sqlite3.Error) as error:
            return types.CallToolResult(is_error=True, content=[types.TextContent(type="text", text=str(error))])
        return types.CallToolResult(structured_content=result, content=[
            types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))
        ])

    return Server("personal-health-agent", on_list_tools=list_tools, on_call_tool=call_tool)


async def serve(database: Path, ds_model: WorkflowModel | None):
    from mcp.server.stdio import stdio_server

    server = create_server(database, ds_model)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser(description="PersonalHealthAgent read-only MCP server over stdio")
    parser.add_argument("--db", required=True, type=Path, help="Existing PersonalHealthAgent SQLite file")
    models = parser.add_mutually_exclusive_group()
    models.add_argument("--model", help="Local Ollama model for the optional DS sandbox tool")
    models.add_argument("--deepseek-env", type=Path, help="Gateway configuration for the optional DS sandbox tool")
    options = parser.parse_args()
    try:
        model = None
        if options.deepseek_env:
            model = DeepSeekWorkflow(options.deepseek_env)
        elif options.model:
            model = Ollama(options.model)
        asyncio.run(serve(options.db.resolve(), model))
    except ImportError:
        parser.exit(1, "Install the MCP dependency with: python -m pip install -e '.[mcp]'\n")
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
