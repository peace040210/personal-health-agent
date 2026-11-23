import argparse
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .coaching import CoachingLoop
from .domain import Metric
from .evidence import EvidenceResearch, EvidenceScope
from .evidence_sources import EvidenceSources
from .knowledge import KnowledgeBase
from .mlx_intent import MlxIntent
from .model import DeepSeekWorkflow, Ollama
from .orchestrator import Orchestrator, Turn
from .presentation import turn_details
from .sdk import HealthTools
from .skill_evolution import evaluate_de, evaluation_summary, evolve_de
from .skills import SkillRegistry
from .store import Store


def display(turn: Turn, trace: bool) -> None:
    print(turn.reply)
    if trace:
        print(json.dumps(turn_details(turn), ensure_ascii=False, default=str, indent=2))


def demo(trace: bool, model: Ollama | None, **backends) -> None:
    examples = Path(__file__).resolve().parent.parent / "examples"
    with closing(Store(":memory:")) as store:
        agent = Orchestrator(store, model, **backends)
        for filename, as_of, questions in (
            ("week_1.csv", date(2026, 11, 15), (
                "看看本周的睡眠趋势",
                "根据本周睡眠记录，给我制定一个改善计划",
                "把我的睡眠目标设为每天 8 小时",
            )),
            ("week_2.csv", date(2026, 11, 22), (
                "比较本周和上周的睡眠时长，先别给我建议",
                "根据本周记录复盘我的睡眠目标",
            )),
        ):
            print(f"\n导入 {filename}，共 {store.import_csv(examples / filename)} 天。")
            for question in questions:
                print(f"\n用户：{question}")
                display(agent.run(question, "demo", as_of), trace)


def pha_demo(trace: bool, model: Ollama | None, **backends) -> None:
    sample = Path(__file__).resolve().parent.parent / "examples" / "google_pha" / "summary.csv"
    with closing(Store(":memory:")) as store:
        count = store.import_csv(sample)
        last_day = store.last_observation(Metric.SLEEP, date.max)
        if last_day is None:
            raise ValueError("官方示例中没有睡眠记录。")
        print(f"导入 Google PHA 官方合成样例，共 {count} 天。演示以数据最后一天 {last_day} 为基准。")
        orchestrator = Orchestrator(store, model, **backends)
        questions = (
            "过去7天我平均睡了多久？",
            "什么是睡眠时长的日均值？",
            "结合过去7天的睡眠记录，帮我制定一个改善计划。",
            "把我的睡眠目标设为每天 8 小时。",
            "复盘过去7天我的睡眠目标。",
        )
        if backends.get("ds_model") and backends.get("evidence"):
            questions += ("过去30天，步数高的日子当晚深睡更长吗，结合研究给我一个建议？",)
        elif backends.get("ds_model"):
            questions += ("过去30天，步数高的日子当晚的深睡时长更长吗？",)
        for question in questions:
            print(f"\n用户：{question}")
            display(orchestrator.run(question, "google-pha-demo", last_day), trace)


def main() -> int:
    parser = argparse.ArgumentParser(description="本地睡眠与运动数据多 Agent 助手")
    parser.add_argument("--db", type=Path, default=Path("health.sqlite3"), help="本地 SQLite 文件")
    parser.add_argument("--model", help="使用已安装的 Ollama 模型，例如 qwen2.5:1.5b")
    parser.add_argument("--deepseek-env", type=Path, help="读取指定的并行智算云配置，使用 DeepSeek V4.1 Flash 进行编排")
    parser.add_argument("--intent-base", type=Path, help="用 MLX 基础权重执行意图识别")
    parser.add_argument("--intent-adapter", type=Path, help="意图识别 LoRA 适配器目录")
    parser.add_argument("--intent-closed-choice", action="store_true", help="在 MLX 意图模型的五个合法标签中直接评分")
    parser.add_argument("--ds-sandbox", action="store_true", help="用配置的模型启用步数与睡眠的 Python 沙箱联合分析")
    parser.add_argument("--de-evidence", action="store_true", help="启用 DE 的外部证据检索和核查")
    parser.add_argument("--evidence-kb", type=Path, help="DE 本地证据知识库的 SQLite 文件")
    parser.add_argument("--coach-loop", action="store_true", help="启用多轮教练记忆和回复前反思")
    parser.add_argument("--tool-calls", action="store_true", help="由模型发出已批准的只读工具调用")
    parser.add_argument("--trace", action="store_true", help="显示意图状态和 Agent 依赖")
    parser.add_argument("--skills", action="store_true", help="启用角色 Skill 和渐进式披露")
    parser.add_argument("--skill-root", type=Path, help="从指定目录读取已选的 Skill 版本")
    parser.add_argument("--skill-version", action="append", default=[], metavar="ROLE=VERSION",
                        help="固定一个角色的 Skill 版本，可重复指定")
    commands = parser.add_subparsers(dest="command", required=True)
    import_parser = commands.add_parser("import", help="导入日记录 CSV 或 Google PHA summary.csv")
    import_parser.add_argument("file", type=Path)
    kb_parser = commands.add_parser("kb-import", help="把 JSONL 证据资料导入本地知识库")
    kb_parser.add_argument("file", type=Path)
    kb_parser.add_argument("--embed-model", help="用本机 Ollama embedding 模型建立向量索引，可选")
    chat_parser = commands.add_parser("chat", help="提问或进入多轮对话")
    chat_parser.add_argument("message", nargs="?", help="省略时使用交互模式")
    chat_parser.add_argument("--session", default="default")
    chat_parser.add_argument("--as-of", type=date.fromisoformat, help="指定用于相对日期解析的日期，格式 YYYY-MM-DD")
    commands.add_parser("demo", help="运行内置虚构数据演示")
    commands.add_parser("pha-demo", help="运行 Google PHA 官方合成样例")
    commands.add_parser("skills-list", help="查看已固定版本的 Skill 元数据")
    standalone = commands.add_parser("skill-run", help="单独运行一个角色 Skill")
    roles = standalone.add_subparsers(dest="role", required=True)
    ds = roles.add_parser("DS", help="直接调用 DS 的只读分析工具")
    ds.add_argument("tool", choices=("query_metric", "review_goal", "analyze_steps_sleep"))
    ds.add_argument("arguments", type=json.loads, help="通过 SDK 工具校验的 JSON 参数")
    de = roles.add_parser("DE", help="直接检索和核查证据")
    hc = roles.add_parser("HC", help="生成一次教练对话草稿")
    for role in (de, hc):
        role.add_argument("message")
        role.add_argument("--metric", choices=tuple(Metric), default=Metric.SLEEP)
    de.add_argument("--joint", action="store_true", help="按步数与当晚睡眠的联合问题检索")
    skill_eval = commands.add_parser("eval-de", help="用固定合成检索回放评测 DE Skill")
    skill_eval.add_argument("--split", choices=("dev", "val", "test"), default="dev")
    evolve = commands.add_parser("evolve-de", help="离线提出、复测和暂存 DE Skill 版本")
    evolve.add_argument("--output", required=True, type=Path)
    evolve.add_argument("--generations", type=int, default=3)
    evolve.add_argument("--candidates", type=int, default=2)
    evolve.add_argument("--no-experience", action="store_true", help="不读取或写入跨次运行的进化经验")
    options = parser.parse_args()
    if options.intent_adapter and not options.intent_base:
        parser.error("--intent-adapter 需要同时指定 --intent-base。")
    if options.intent_closed_choice and not options.intent_base:
        parser.error("--intent-closed-choice 需要同时指定 --intent-base。")
    if options.ds_sandbox and not (options.deepseek_env or options.model):
        parser.error("--ds-sandbox 需要 --deepseek-env 或 --model。")
    if options.tool_calls and not (options.deepseek_env or options.model):
        parser.error("--tool-calls 需要 --deepseek-env 或 --model。")
    if options.command == "evolve-de" and not (options.deepseek_env or options.model):
        parser.error("evolve-de 需要 --deepseek-env 或 --model。")

    try:
        versions = {}
        for assignment in options.skill_version:
            role, separator, version = assignment.partition("=")
            if not separator or not version or role in versions:
                raise ValueError("Skill 版本请按 DS=v1、DE=v1 或 HC=v1 指定，每个角色最多一次。")
            versions[role] = version
        skills = SkillRegistry(options.skill_root, versions) if (
            options.skills or options.skill_root or versions
            or options.command in {"skills-list", "skill-run", "eval-de", "evolve-de"}
        ) else None
        if options.command == "skills-list":
            print(json.dumps([asdict(item) for item in skills.metadata], ensure_ascii=False, indent=2))
            return 0
        if options.command == "eval-de":
            print(json.dumps(evaluation_summary(evaluate_de(skills, options.split)), ensure_ascii=False, indent=2))
            return 0
        model = Ollama(options.model) if options.model else None
        workflow_model = DeepSeekWorkflow(options.deepseek_env) if options.deepseek_env else None
        if options.command == "evolve-de":
            report = evolve_de(workflow_model or model, skills, options.output, generations=options.generations,
                               candidates=options.candidates, use_experience=not options.no_experience)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return int(report["status"] in {"no_valid_proposals", "holdout_rejected"})
        knowledge = KnowledgeBase(options.evidence_kb) if options.evidence_kb else None
        if options.command == "kb-import":
            if knowledge is None:
                parser.error("kb-import 需要指定 --evidence-kb。")
            sources, passages = knowledge.import_jsonl(options.file, embedding_model=options.embed_model)
            print(f"已更新 {sources} 个来源，索引 {passages} 个证据片段。")
            return 0
        if knowledge is not None and not knowledge.path.is_file():
            raise ValueError("找不到本地证据知识库，请先用 kb-import 导入资料。")
        if options.command == "skill-run":
            session = skills.start()
            skill = session.activate(options.role)
            backend = workflow_model or model
            if options.role == "DS":
                if options.tool == "analyze_steps_sleep" and backend is None:
                    parser.error("analyze_steps_sleep 需要 --deepseek-env 或 --model。")
                with closing(Store(options.db)) as store:
                    result = HealthTools(store, ds_model=backend).execute(options.tool, options.arguments, skill=skill)
            elif options.role == "DE":
                scope = EvidenceScope.from_question(Metric(options.metric), options.message, joint=options.joint)
                result = EvidenceResearch(EvidenceSources.from_environment(), backend, knowledge).run(scope, skill=skill)
            else:
                result = CoachingLoop(backend).draft(options.message, Metric(options.metric), None, skill=skill)
            payload = {"result": asdict(result)}
            if options.trace:
                payload["skill_events"] = [asdict(event) for event in session.events]
            print(json.dumps(payload, ensure_ascii=False, default=str, indent=2))
            return 0
        backends = {
            "intent_model": MlxIntent(options.intent_base, options.intent_adapter,
                                      closed_choice=options.intent_closed_choice) if options.intent_base else None,
            "workflow_model": workflow_model,
            "ds_model": (workflow_model or model) if options.ds_sandbox else None,
            "tool_model": (workflow_model or model) if options.tool_calls else None,
            "evidence": EvidenceResearch(EvidenceSources.from_environment(), workflow_model or model, knowledge)
            if options.de_evidence or knowledge else None,
            "coach_loop": options.coach_loop,
            "skills": skills,
        }
        if options.command == "demo":
            demo(options.trace, model, **backends)
            return 0
        if options.command == "pha-demo":
            pha_demo(options.trace, model, **backends)
            return 0
        with closing(Store(options.db)) as store:
            if options.command == "import":
                result = HealthTools(store).call("import_daily_records", {"path": str(options.file)})
                print(f"已导入 {result['days']} 天记录。")
                return 0
            agent = Orchestrator(store, model, **backends)
            if options.message is not None:
                today = options.as_of or datetime.now(ZoneInfo("Asia/Shanghai")).date()
                display(agent.run(options.message, options.session, today), options.trace)
                return 0
            print("输入睡眠、步数或目标问题。输入 /exit 结束。")
            while True:
                try:
                    message = input("你：").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return 0
                if message == "/exit":
                    return 0
                if message:
                    today = options.as_of or datetime.now(ZoneInfo("Asia/Shanghai")).date()
                    display(agent.run(message, options.session, today), options.trace)
    except (ValueError, ImportError, OSError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
