"""FastAPI demo adapter. Run from the project checkout with python -m health_agent.web."""

import argparse
import asyncio
import json
import logging
import shutil
from collections.abc import Callable
from contextlib import asynccontextmanager, closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Literal
from uuid import UUID, uuid4
from weakref import WeakValueDictionary
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .domain import Metric
from .ds_sandbox import PythonSandbox
from .evidence import EvidenceResearch
from .evidence_sources import EvidenceSources, SourceUnavailable
from .knowledge import KnowledgeBase
from .model import DeepSeekWorkflow, Ollama, ToolModel, WorkflowModel
from .orchestrator import Orchestrator, RunProgress
from .presentation import turn_details
from .qibu import ActionConflict, QibuAgent, QibuTools, Reply, lunch_budget_declaration
from .qibu_memory import MemoryConflict, QibuMemory
from .skills import SkillRegistry
from .store import Store

_PROJECT = Path(__file__).resolve().parent.parent
_SAMPLES = Path(__file__).with_name("web_samples")
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebConfig:
    project: Path = _PROJECT
    frontend: Path | None = None
    model: WorkflowModel | None = None
    tool_model: ToolModel | None = None
    online_evidence: bool = False
    skills: SkillRegistry | None = None
    qibu_dir: Path | None = None
    qibu_home: bool = False


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID
    message: str = Field(min_length=1, max_length=1200)

    @field_validator("message")
    @classmethod
    def trim_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("请输入一个问题。")
        return value.strip()


class QibuChatRequest(ChatRequest):
    turn_id: UUID | None = None


class QibuActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID
    action_id: UUID
    operation: Literal["confirm", "complete", "undo", "remove"]
    day: date | None = None


class QibuMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID
    operation: Literal["confirm", "decline", "forget"]
    draft_id: UUID | None = None


class LocalEvidenceOnly:
    web_key = ""

    def pubmed(self, query: str):
        raise SourceUnavailable("在线检索未启用")

    medlineplus = pubmed

    def data_commons(self, query: str, entity: str, place: str):
        raise SourceUnavailable("在线检索未启用")


def _read_sample(name: str) -> dict:
    return json.loads((_SAMPLES / name).read_text(encoding="utf-8"))


class DemoRuntime:
    def __init__(self, config: WebConfig, directory: Path):
        self.config = config
        self.directory = directory
        self.seed = directory / "seed.sqlite3"
        examples = config.project / "examples"
        with closing(Store(self.seed)) as store:
            self.days = store.import_csv(examples / "google_pha" / "summary.csv")
            self.as_of = store.last_observation(Metric.SLEEP, date.max)
        if self.as_of is None:
            raise ValueError("公开演示数据里没有睡眠记录。")
        self.sessions: dict[UUID, Lock] = {}
        self.qibu_sessions: WeakValueDictionary[UUID, Lock] = WeakValueDictionary()
        self.sessions_lock = Lock()
        self.qibu_dir = config.qibu_dir or directory / "qibu"
        self.qibu_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.memory = QibuMemory(self.qibu_dir)
        self.skills = config.skills or SkillRegistry()
        self.knowledge = KnowledgeBase(directory / "evidence.sqlite3")
        self.knowledge.import_jsonl(examples / "evidence_kb.jsonl")
        self.sources = EvidenceSources.from_environment() if config.online_evidence else LocalEvidenceOnly()
        self.replay = _read_sample("joint.json")
        self.evolution = _read_sample("evolution.json")

    @contextmanager
    def session_store(self, session: UUID, *, qibu: bool = False):
        directory = self.qibu_dir if qibu else self.directory
        sessions = self.qibu_sessions if qibu else self.sessions
        database = directory / f"{session}.sqlite3"
        with self.sessions_lock:
            lock = sessions.get(session)
            if lock is None:
                if len(sessions) >= 64:
                    raise HTTPException(429, "演示会话已达上限。")
                if not database.exists():
                    shutil.copyfile(self.seed, database)
                    database.chmod(0o600)
                lock = Lock()
                sessions[session] = lock
        with lock, closing(Store(database)) as store:
            yield store

    def core(self, store: Store, evidence: EvidenceResearch | None = None) -> Orchestrator:
        model = self.config.model
        return Orchestrator(
            store, workflow_model=model, ds_model=model, tool_model=self.config.tool_model,
            evidence=evidence or EvidenceResearch(self.sources, model, self.knowledge),
            coach_loop=True, skills=self.skills,
        )

    def chat(self, session: UUID, message: str, on_progress: Callable[[RunProgress], None] | None = None) -> dict:
        with self.session_store(session) as store:
            agent = self.core(store)
            turn = agent.run(message, str(session), self.as_of, on_progress=on_progress)
            return {"mode": "live", "question": message, "reply": turn.reply,
                    "details": turn_details(turn, include_code=False)}


def create_app(config: WebConfig = WebConfig()) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with TemporaryDirectory(prefix="personal-health-demo-") as directory:
            app.state.demo = DemoRuntime(config, Path(directory))
            yield

    app = FastAPI(title="PersonalHealthAgent Demo", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def qibu_device(request: Request, call_next):
        if not request.url.path.startswith("/api/qibu/"):
            return await call_next(request)
        memories = app.state.demo.memory
        owner, cookie = memories.identify(request.cookies.get(memories.cookie_name, ""))
        request.state.qibu_owner = owner
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        if cookie is not None:
            response.set_cookie(memories.cookie_name, cookie, max_age=365 * 86400, httponly=True,
                                secure=request.url.scheme == "https", samesite="lax", path="/api/qibu/")
        return response

    @app.get("/api/demo")
    def overview():
        demo = app.state.demo
        return {
            "dataset": {"days": demo.days, "as_of": demo.as_of, "synthetic": True},
            "capabilities": {"joint_live": config.model is not None and PythonSandbox().available,
                             "model": config.model is not None, "online_evidence": config.online_evidence},
            "replay": demo.replay,
        }

    @app.post("/api/chat")
    def chat(body: ChatRequest):
        return app.state.demo.chat(body.session_id, body.message)

    @app.post("/api/chat/stream")
    async def stream_chat(body: ChatRequest):
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()

        def push(event: RunProgress) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, ("progress", asdict(event)))

        async def produce() -> None:
            try:
                result = await run_in_threadpool(app.state.demo.chat, body.session_id, body.message, push)
                queue.put_nowait(("done", result))
            except HTTPException as error:
                queue.put_nowait(("error", {"detail": error.detail}))
            except Exception:
                _LOGGER.exception("Web demo turn failed")
                queue.put_nowait(("error", {"detail": "运行未完成，请重试。"}))
            finally:
                queue.put_nowait(("end", None))

        async def stream():
            producer = asyncio.create_task(produce())
            try:
                while True:
                    event, value = await queue.get()
                    if event == "end":
                        break
                    payload = json.dumps(jsonable_encoder(value), ensure_ascii=False)
                    yield f"event: {event}\ndata: {payload}\n\n"
            finally:
                await producer

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/evolution")
    def evolution():
        return app.state.demo.evolution

    def today() -> date:
        return datetime.now(ZoneInfo("Asia/Shanghai")).date()

    def owned(request: Request, session: UUID, *, create: bool = True):
        runtime = app.state.demo
        owner = request.state.qibu_owner
        try:
            runtime.memory.bind(owner, session, create=create)
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        return runtime, owner

    @app.get("/api/qibu/conversations")
    def qibu_conversations(request: Request):
        return app.state.demo.memory.conversations(request.state.qibu_owner)

    @app.post("/api/qibu/conversations")
    def qibu_new_conversation(request: Request):
        session = uuid4()
        owned(request, session)
        return {"id": str(session)}

    @app.get("/api/qibu/session")
    def qibu_session(session_id: UUID, request: Request):
        runtime, owner = owned(request, session_id)
        day = today()
        with runtime.session_store(session_id, qibu=True) as store:
            tools = QibuTools(store)
            return {"as_of": runtime.as_of, "today": day, "model_configured": config.model is not None,
                    "actions": tools.actions(day), "history": tools.history(day, runtime.memory.draft_status(owner)),
                    "memory": runtime.memory.get(owner)}

    @app.post("/api/qibu/chat")
    def qibu_chat(body: QibuChatRequest, request: Request):
        runtime, owner = owned(request, body.session_id)
        with runtime.session_store(body.session_id, qibu=True) as store:
            tools = QibuTools(store)
            day = today()
            turn_id = str(body.turn_id) if body.turn_id else None
            try:
                previous = tools.previous_reply(body.message, turn_id)
            except ActionConflict as error:
                raise HTTPException(409, str(error)) from error
            if previous is not None:
                result = tools.reply_state(previous, day, runtime.memory.draft_status(owner))
                return {**result, "pending_draft_id": tools.current_draft_id(day)}
            remembered = runtime.memory.get(owner)
            minutes = lunch_budget_declaration(body.message)
            if minutes:
                draft = runtime.memory.propose(owner, body.session_id, minutes)
                reply = Reply(f"记住工作日午休最多 {minutes} 分钟？",
                              ("确认后，之后的会话可以用这个限制安排午休或午间行动。",),
                              intent="memory", memory_draft=draft)
            else:
                evidence = EvidenceResearch(runtime.sources, config.model, runtime.knowledge)
                agent = QibuAgent(tools, runtime.core(store, evidence), evidence, runtime.skills, config.model,
                                  (remembered or {}).get("minutes"))
                reply = agent.run(body.message, str(body.session_id), runtime.as_of, day)
            result = asdict(reply)
            tools.record(body.message, result, turn_id)
            runtime.memory.touch(owner, body.session_id, body.message)
            return {**result, "pending_draft_id": tools.current_draft_id(day)}

    @app.post("/api/qibu/actions")
    def qibu_action(body: QibuActionRequest, request: Request):
        runtime, _ = owned(request, body.session_id, create=False)
        with runtime.session_store(body.session_id, qibu=True) as store:
            tools = QibuTools(store)
            action_date = today()
            try:
                item = tools.change(str(body.action_id), body.operation, action_date, body.day)
            except LookupError as error:
                raise HTTPException(404, str(error)) from error
            except ActionConflict as error:
                raise HTTPException(409, str(error)) from error
            return {"action": item, "actions": tools.actions(action_date), "today": action_date}

    @app.post("/api/qibu/memory")
    def qibu_memory(body: QibuMemoryRequest, request: Request):
        runtime, owner = owned(request, body.session_id, create=False)
        if body.operation != "forget" and body.draft_id is None:
            raise HTTPException(422, "请选择一条记忆提议。")
        try:
            runtime.memory.change(owner, body.session_id, body.operation, body.draft_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except MemoryConflict as error:
            raise HTTPException(409, str(error)) from error
        return {"memory": runtime.memory.get(owner)}

    consumer = config.project / "user-web" / "dist"
    if (consumer / "index.html").is_file():
        app.mount("/qibu", StaticFiles(directory=consumer, html=True), name="qibu")
        if config.qibu_home:
            @app.get("/", include_in_schema=False)
            def qibu_root():
                return RedirectResponse("/qibu/")
    frontend = config.frontend or config.project / "web" / "dist"
    if (frontend / "index.html").is_file():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="demo")
    elif not (config.qibu_home and (consumer / "index.html").is_file()):
        @app.get("/", include_in_schema=False)
        def root():
            return RedirectResponse("/docs")

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="运行 PersonalHealthAgent 的本地 Web 演示")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    model_options = parser.add_mutually_exclusive_group()
    model_options.add_argument("--model", help="Ollama 模型名")
    model_options.add_argument("--deepseek-env", type=Path, help="本地 DeepSeek 配置文件")
    parser.add_argument("--online-evidence", action="store_true", help="允许检索外部公开证据")
    parser.add_argument("--skill-root", type=Path)
    parser.add_argument("--de-version", default="v1")
    parser.add_argument("--qibu-data-dir", type=Path, help="为栖步保存会话与行动的本地目录")
    parser.add_argument("--qibu-home", action="store_true", help="根路径打开栖步用户端")
    options = parser.parse_args()
    model = Ollama(options.model) if options.model else None
    if options.deepseek_env:
        model = DeepSeekWorkflow(options.deepseek_env)
    skills = SkillRegistry(options.skill_root, {"DE": options.de_version})
    config = WebConfig(model=model, tool_model=model, online_evidence=options.online_evidence, skills=skills,
                       qibu_dir=options.qibu_data_dir, qibu_home=options.qibu_home)
    uvicorn.run(create_app(config), host=options.host, port=options.port, proxy_headers=False)


if __name__ == "__main__":
    main()
