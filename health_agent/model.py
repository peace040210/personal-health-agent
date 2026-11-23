import json
import re
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.error import URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener


class IntentModel(Protocol):
    def choose(self, system: str, payload: dict, choices: tuple[str, ...]) -> str | None: ...


class WorkflowModel(Protocol):
    def structured(self, system: str, payload: dict, schema: dict, *, max_tokens: int = 512) -> dict | None: ...


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict


class ToolModel(Protocol):
    def tool_calls(self, system: str, payload: dict, tools: list[dict]) -> tuple[ToolCall, ...] | None: ...


def _messages(system: str, payload: dict) -> list[dict]:
    return [{"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def _content(message: dict | None) -> dict | None:
    try:
        result = json.loads(message["content"]) if message else None
        return result if isinstance(result, dict) else None
    except (KeyError, TypeError, ValueError):
        return None


def _tool_calls(message: dict | None) -> tuple[ToolCall, ...] | None:
    if message is None:
        return None
    calls = message.get("tool_calls", [])
    if not isinstance(calls, list):
        return None
    try:
        parsed = []
        for call in calls:
            function = call["function"]
            name, arguments = function["name"], function["arguments"]
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return None
            parsed.append(ToolCall(name, arguments))
        return tuple(parsed)
    except (KeyError, TypeError, ValueError):
        return None


class Ollama:
    """Run a bounded, schema-constrained decision on the local Ollama server."""

    def __init__(self, model: str):
        self.model = model
        self.opener = build_opener(ProxyHandler({}))

    def choose(self, system: str, payload: dict, choices: tuple[str, ...]) -> str | None:
        schema = {
            "type": "object", "required": ["choice"], "additionalProperties": False,
            "properties": {"choice": {"type": "string", "enum": list(choices)}},
        }
        result = self.structured(system + "\n可选 JSON 值：" + "、".join(choices), payload, schema, max_tokens=64)
        if result and set(result) == {"choice"} and result["choice"] in choices:
            return result["choice"]
        return None

    def structured(self, system: str, payload: dict, schema: dict, *, max_tokens: int = 512) -> dict | None:
        return _content(self._chat(system, payload, max_tokens, format=schema))

    def tool_calls(self, system: str, payload: dict, tools: list[dict]) -> tuple[ToolCall, ...] | None:
        return _tool_calls(self._chat(system, payload, 512, tools=tools))

    def _chat(self, system: str, payload: dict, max_tokens: int, **options) -> dict | None:
        body = {
            "model": self.model,
            "messages": _messages(system, payload),
            "stream": False, "options": {"temperature": 0, "num_predict": max_tokens}, **options,
        }
        request = Request(
            "http://127.0.0.1:11434/api/chat", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with self.opener.open(request, timeout=30 if max_tokens > 512 else 12) as response:
                result = json.load(response)["message"]
            return result if isinstance(result, dict) else None
        except (URLError, TimeoutError, OSError, ValueError, KeyError, TypeError):
            return None


class DeepSeekWorkflow:
    """Use DeepSeek through the configured Paratera gateway."""

    BASE_URL = "https://llmapi.paratera.com/v1"
    MODEL = "DeepSeek-V4.1-Flash"

    def __init__(self, env_path: Path):
        keys = {"OPENAI_API_KEY", "OPENAI_BASE_URL"}
        configuration = {
            match[1]: match[2].strip().strip('"\'')
            for match in re.finditer(r"^(?:export\s+)?([A-Z_0-9]+)\s*=\s*([^\r\n]*)", env_path.read_text(encoding="utf-8"), re.M)
            if match[1] in keys
        }
        if configuration.get("OPENAI_BASE_URL", "").rstrip("/") != self.BASE_URL:
            raise ValueError("配置中的网关地址与已配置的 DeepSeek 网关不一致。")
        self._token = configuration.get("OPENAI_API_KEY", "")
        if not self._token:
            raise ValueError("配置文件缺少 OPENAI_API_KEY。")
        context = ssl.create_default_context()
        system_bundle = Path("/etc/ssl/cert.pem")
        if system_bundle.is_file():
            context.load_verify_locations(cafile=str(system_bundle))
        self.opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context))

    def structured(self, system: str, payload: dict, schema: dict, *, max_tokens: int = 640) -> dict | None:
        system += "\n输出须符合以下 JSON Schema：\n" + json.dumps(schema, ensure_ascii=False)
        return _content(self._chat(system, payload, max_tokens, response_format={"type": "json_object"}))

    def tool_calls(self, system: str, payload: dict, tools: list[dict]) -> tuple[ToolCall, ...] | None:
        return _tool_calls(self._chat(system, payload, 640, tools=tools, tool_choice="required"))

    def _chat(self, system: str, payload: dict, max_tokens: int, **options) -> dict | None:
        body = {
            "model": self.MODEL,
            "messages": _messages(system, payload),
            "temperature": 0, "max_tokens": max_tokens,
            "thinking": {"type": "disabled"}, **options,
        }
        request = Request(self.BASE_URL + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        request.add_unredirected_header("Authorization", f"Bearer {self._token}")
        try:
            with self.opener.open(request, timeout=35 if max_tokens > 640 else 15) as response:
                result = json.load(response)["choices"][0]["message"]
            return result if isinstance(result, dict) else None
        except (URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError):
            return None
