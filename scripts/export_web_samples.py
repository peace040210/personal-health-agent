"""Export browser replays from public synthetic records and an existing DE experiment."""

import argparse
import json
from contextlib import closing
from dataclasses import asdict
from datetime import date
from pathlib import Path

from health_agent.domain import Metric
from health_agent.evidence import EvidenceResearch
from health_agent.evidence_sources import EvidenceSources
from health_agent.model import DeepSeekWorkflow
from health_agent.orchestrator import Orchestrator
from health_agent.presentation import turn_details
from health_agent.skill_evolution import evaluate_de
from health_agent.skills import SkillRegistry
from health_agent.store import Store

_PROJECT = Path(__file__).resolve().parent.parent
_OUT = _PROJECT / "health_agent" / "web_samples"
_QUESTION = "过去30天，步数高的日子当晚深睡更长吗，结合研究给我一个建议？"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(name: str, payload: dict) -> None:
    _OUT.mkdir(exist_ok=True)
    (_OUT / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _examples(rows):
    examples = {}
    for row in rows:
        examples.setdefault(row["family"], row)
    return examples


def export_evolution(run: Path, parent: SkillRegistry) -> str:
    report = _read(run / "report.json")
    version = report["released_version"]
    if version is None:
        raise ValueError("请选择有通过评测版本的实验。")
    baseline = {}
    for split in ("dev", "val", "test"):
        rows = evaluate_de(parent, split)
        if sum(row.passed for row in rows) != report["baseline"][split]["passed"]:
            raise ValueError("当前指定的父版本无法复现实验基线。")
        baseline.update(_examples(asdict(row) | {"passed": row.passed, "split": split} for row in rows))
    held_out = _examples(_read(run / "holdout.json")["probed"])
    candidates = []
    selected_round = next(round_ for round_ in report["rounds"] if round_["selected"] == version)
    for choice in selected_round["candidates"]:
        if choice["status"] not in {"selected", "eligible", "rejected"}:
            continue
        tag = choice["version"]
        probe = _read(run / "candidates" / tag / "probe.json")
        examples = _examples(probe["cases"])
        if tag == version:
            examples.update(held_out)
        candidates.append({**choice, "cases": examples, "regressions": probe["val_regression_cases"]})
    if version not in {item["version"] for item in candidates}:
        raise ValueError("实验缺少入选版本的候选记录。")
    _write("evolution.json", {
        "run": report["run"], "version": version, "status": report["status"],
        "baseline": report["baseline"], "final": report["final"],
        "cases": baseline, "candidates": candidates, "lessons": report["lessons"],
    })
    return version


def export_joint(env: Path, root: Path, version: str) -> None:
    model = DeepSeekWorkflow(env)
    skills = SkillRegistry(root, {"DE": version})
    with closing(Store(":memory:")) as store:
        store.import_csv(_PROJECT / "examples" / "google_pha" / "summary.csv")
        day = store.last_observation(Metric.SLEEP, date.max)
        agent = Orchestrator(
            store, ds_model=model, workflow_model=model, tool_model=model, coach_loop=True,
            evidence=EvidenceResearch(EvidenceSources(), model), skills=skills,
        )
        turn = agent.run(_QUESTION, "public-web-replay", day)
    if (not turn.sandbox_analyses or turn.sandbox_analyses[0].status != "ok"
            or not turn.evidence_runs or not turn.evidence_runs[0].investigations or not turn.coaching_turns):
        raise ValueError("DS、DE、HC 的回放尚未完整，未导出演示。")
    _write("joint.json", {"mode": "replay", "question": _QUESTION, "reply": turn.reply,
                          "details": turn_details(turn, include_code=False)})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--deepseek-env", type=Path)
    args = parser.parse_args()
    run = args.run.resolve()
    release = _read(run / "report.json")
    if not release["lessons"] or not release["released_version"]:
        raise ValueError("请选择有通过评测版本的实验。")
    root = run.parent.parent
    parent = SkillRegistry(root, {"DE": release["lessons"][0]["from_version"]})
    version = export_evolution(run, parent)
    if args.deepseek_env:
        export_joint(args.deepseek_env, root, version)


if __name__ == "__main__":
    main()
