"""Offline DE skill evolution against fixed, synthetic provider replays."""

import json
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from .domain import Metric
from .evidence import EvidenceResearch, EvidenceScope
from .evidence_sources import EvidenceDocument
from .model import WorkflowModel
from .skills import DE_SEARCH_ORDERS, EvidenceSkillPolicy, SkillRegistry

_CASES = Path(__file__).with_name("evals") / "de_skill_cases.json"
_SPLITS = ("dev", "val", "test")
_AMENDMENT = "\n\n## 联合检索规则\n"
_PROPOSE = """你为健康 Agent 的 DE Skill 提出离线检索策略候选。
根据失败轨迹归因，给出互不相同的策略，每项只改一个白名单字段 joint_search_order。
候选要能解决当前失败，也要考虑已正确命中严格查询的场景会不会退化。
指导文字只解释这条联合检索规则及适用条件，禁止更改来源核查、引文要求、工具或权限。
把过往经验当作历史观察，结合本轮的案例判断。只能输出 JSON。"""


@dataclass(frozen=True)
class Evaluation:
    case: str
    family: str
    question: str
    expected: str
    actual: str
    guard_passed: bool
    searches: tuple[dict, ...]
    findings: tuple[dict, ...]
    limitations: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.expected == self.actual and self.guard_passed


class ReplaySources:
    web_key = ""

    def __init__(self, family: dict):
        self.docs = {
            key: EvidenceDocument(
                "MedlinePlus.gov" if key == "guidance" else "PubMed", title,
                f"https://example.invalid/de-replay/{family['id']}/{key}", content,
            )
            for key, (title, content) in family["responses"].items()
        }

    def pubmed(self, term: str) -> tuple[EvidenceDocument, ...]:
        if '"same night"[tiab]' in term:
            key = "strict"
        elif term.startswith('("step count"[tiab]'):
            key = "metrics"
        else:
            key = "broader"
        return (self.docs[key],) if key in self.docs else ()

    def medlineplus(self, term: str) -> tuple[EvidenceDocument, ...]:
        return (self.docs["guidance"],) if "guidance" in self.docs else ()


def evaluate_de(registry: SkillRegistry, split: str) -> tuple[Evaluation, ...]:
    if split not in _SPLITS:
        raise ValueError("评测切分只能是 dev、val 或 test。")
    cases = json.loads(_CASES.read_text(encoding="utf-8"))
    evaluations = []
    for family in cases:
        if family["split"] != split:
            continue
        replay = ReplaySources(family)
        docs = {doc.url: doc for doc in replay.docs.values()}
        for index, question in enumerate(family["questions"], 1):
            scope = EvidenceScope.from_question(Metric.SLEEP, question, joint=family.get("joint", True))
            report = EvidenceResearch(replay).run(scope, skill=registry.start().activate("DE"))
            actual = ("direct" if any(item.fit == "direct" for item in report.findings)
                      else "context" if report.findings else "insufficient")
            sources_valid = all(
                item.url in docs and item.source == docs[item.url].source and item.title == docs[item.url].title
                and item.quote and item.quote.removesuffix("…") in docs[item.url].text
                and len(item.quote.split()) <= 25
                for item in report.findings
            )
            guard = (sources_valid and (actual != "direct" or family["expect"] == "direct")
                     and (not scope.joint or any("不能据此判断" in note for note in report.limitations))
                     and max((item.round for item in report.investigations), default=0) <= 2)
            evaluations.append(Evaluation(f"{family['id']}-{index}", family["id"], question, family["expect"],
                                          actual, guard, tuple(asdict(item) for item in report.investigations),
                                          tuple(asdict(item) for item in report.findings), report.limitations))
    return tuple(evaluations)


def evaluation_summary(results: tuple[Evaluation, ...]) -> dict:
    return {
        "passed": sum(item.passed for item in results),
        "total": len(results),
        "families": len({item.family for item in results}),
        "guard_failures": sum(not item.guard_passed for item in results),
        "search_calls": sum(len(item.searches) for item in results),
    }


def _regressions(before: tuple[Evaluation, ...], after: tuple[Evaluation, ...]) -> list[str]:
    accepted = {item.case for item in before if item.passed}
    return [item.case for item in after if item.case in accepted and not item.passed]


def _feedback(results: tuple[Evaluation, ...]) -> list[dict]:
    examples = {}
    for item in results:
        examples.setdefault((item.family, item.passed), item)
    return [
        {"question": row.question, "expected": row.expected, "actual": row.actual,
         "searches": row.searches, "findings": [{"title": item["title"], "fit": item["fit"]}
                                               for item in row.findings]}
        for row in sorted(examples.values(), key=lambda item: item.passed)
    ]


def _schema(count: int) -> dict:
    fields = {
        "joint_search_order": {"enum": list(DE_SEARCH_ORDERS)},
        "guidance": {"type": "string", "minLength": 1, "maxLength": 600},
        "rationale": {"type": "string", "minLength": 1, "maxLength": 600},
    }
    return {"type": "object", "required": ["candidates"], "additionalProperties": False,
            "properties": {"candidates": {"type": "array", "minItems": 1, "maxItems": count, "items": {
                "type": "object", "required": list(fields), "additionalProperties": False, "properties": fields,
            }}}}


def _candidate(value: object) -> tuple[EvidenceSkillPolicy, str, str]:
    if not isinstance(value, dict) or value.keys() != {"joint_search_order", "guidance", "rationale"}:
        raise ValueError("候选字段超出允许范围。")
    policy = EvidenceSkillPolicy.parse({"joint_search_order": value["joint_search_order"]})
    guidance, rationale = value["guidance"], value["rationale"]
    if any(not isinstance(item, str) or not 1 <= len(item.strip()) <= 600 for item in (guidance, rationale)):
        raise ValueError("候选说明长度不符合约定。")
    if re.search(r"[\r\n<>]|\]\(", guidance):
        raise ValueError("Skill 修改只接受单段说明。")
    return policy, guidance.strip(), rationale.strip()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _materialize(root: Path, version: str, base_body: str, reference: str, description: str,
                 policy: EvidenceSkillPolicy, guidance: str) -> SkillRegistry:
    directory = root / version / "domain-expert"
    references = directory / "references"
    references.mkdir(parents=True)
    body = base_body.split(_AMENDMENT, 1)[0] + _AMENDMENT + guidance
    (directory / "SKILL.md").write_text(
        f"---\nname: domain-expert\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8",
    )
    _write_json(directory / "policy.json", asdict(policy))
    (references / "source-fit.md").write_text(reference, encoding="utf-8")
    return SkillRegistry(root, {"DE": version})


def evolve_de(model: WorkflowModel, registry: SkillRegistry, output: Path, *,
              generations: int = 3, candidates: int = 2, use_experience: bool = True) -> dict:
    if not 1 <= generations <= 5 or not 1 <= candidates <= 3:
        raise ValueError("每次进化允许 1–5 代，每代 1–3 个候选。")
    now = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_id = f"de-{now}-{uuid4().hex[:6]}"
    run_path = output / "runs" / run_id
    run_path.mkdir(parents=True)
    candidate_root = run_path / "candidates"
    experience = []
    experience_root = output / "experience"
    if use_experience:
        for path in sorted(experience_root.glob("de-*.json"))[-8:]:
            experience.extend(json.loads(path.read_text(encoding="utf-8")))
    previous_lessons = len(experience)
    learned = []
    baseline_skill = registry.start().activate("DE")
    current_policy = baseline_skill.evidence_policy()
    version = baseline_skill.metadata.version
    initial_dev, initial_val = evaluate_de(registry, "dev"), evaluate_de(registry, "val")
    dev, val = initial_dev, initial_val
    rounds = []
    winner = None
    had_proposals = False
    for generation in range(1, generations + 1):
        result = model.structured(_PROPOSE, {
            "parent_version": version,
            "parent_policy": asdict(current_policy),
            "current_skill": winner[0].start().activate("DE").body if winner else baseline_skill.body,
            "dev_execution_feedback": _feedback(dev),
            "experience": experience[-8:],
            "previous_dev_probes": [
                {"policy": probe["policy"], "dev": probe["dev"]}
                for probe in rounds[-1]["candidates"] if "dev" in probe
            ] if rounds else [],
            "strategy_options": {
                "legacy": "第一轮严格时序和指标，下一轮转向广义活动；带模型时可从每轮预允许的选项中选一个。",
                "metrics-first": "第一轮仅按步数和睡眠指标搜索，下一轮才查广义活动。",
                "retry-without-timing": "第一轮严格匹配时序和指标，未得到直接证据时仅放宽时序检索过滤，保留步数与结局。",
            },
            "request": f"提出最多 {candidates} 个与父版本不同的策略，按预计收益排序。",
        }, _schema(candidates), max_tokens=1100)
        proposals = result.get("candidates") if isinstance(result, dict) else None
        proposal_records = []
        choices = []
        seen = {current_policy}
        valid_list = isinstance(proposals, list) and 1 <= len(proposals) <= candidates
        for index, entry in enumerate(proposals if valid_list else [], 1):
            tag = f"{run_id}-g{generation}-c{index}"
            try:
                policy, guidance, rationale = _candidate(entry)
                if policy in seen:
                    raise ValueError("该代已比较同一策略。")
            except ValueError as error:
                proposal_records.append({"version": tag, "status": "invalid", "reason": str(error)})
                continue
            seen.add(policy)
            had_proposals = True
            trial = _materialize(candidate_root, tag, baseline_skill.body, baseline_skill.reference("source-fit.md"),
                                 baseline_skill.metadata.description, policy, guidance)
            trial_dev, trial_val = evaluate_de(trial, "dev"), evaluate_de(trial, "val")
            dev_score, val_score = evaluation_summary(trial_dev), evaluation_summary(trial_val)
            regressions = _regressions(val, trial_val)
            eligible = (dev_score["passed"] > evaluation_summary(dev)["passed"] and not regressions
                        and not dev_score["guard_failures"] and not val_score["guard_failures"])
            record = {"version": tag, "status": "eligible" if eligible else "rejected", "policy": asdict(policy),
                      "rationale": rationale, "dev": dev_score, "val": val_score, "val_regressions": len(regressions)}
            proposal_records.append(record)
            _write_json(candidate_root / tag / "probe.json",
                        {**record, "val_regression_cases": regressions,
                         "cases": [asdict(item) | {"passed": item.passed} for item in trial_dev + trial_val]})
            if eligible:
                choices.append((trial, trial_dev, trial_val, policy, record))
        picked = None
        if choices:
            winner = max(choices, key=lambda item: (item[4]["dev"]["passed"], -item[4]["dev"]["search_calls"]))
            _, winning_dev, val, current_policy, record = winner
            recovered = {item.case for item in dev if not item.passed}
            repaired_families = sorted({item.family for item in winning_dev if item.passed and item.case in recovered})
            lesson = {"from_version": version, "version": record["version"], "policy": asdict(current_policy),
                      "what_changed": record["rationale"], "recovered_families": repaired_families,
                      "dev_before": evaluation_summary(dev), "dev_after": record["dev"], "val_after": record["val"]}
            learned.append(lesson)
            if use_experience:
                experience.append(lesson)
            version = picked = record["version"]
            dev = winning_dev
            record["status"] = "selected"
        rounds.append({"generation": generation, "proposal_status": "received" if valid_list else "unavailable",
                       "candidates": proposal_records, "selected": picked})

    initial_test = evaluate_de(registry, "test")
    final_registry = winner[0] if winner else registry
    test = evaluate_de(final_registry, "test") if winner else initial_test
    test_regressions = _regressions(initial_test, test)
    release = bool(winner and not test_regressions and not evaluation_summary(test)["guard_failures"])
    report = {
        "run": run_id, "run_path": str(run_path.resolve()), "used_prior_lessons": previous_lessons,
        "status": ("released" if release else "holdout_rejected" if winner else
                   "no_improvement" if had_proposals else "no_valid_proposals"),
        "baseline": {split: evaluation_summary(rows)
                     for split, rows in zip(_SPLITS, (initial_dev, initial_val, initial_test))},
        "final": {split: evaluation_summary(rows) for split, rows in zip(_SPLITS, (dev, val, test))},
        "probed_version": version, "released_version": version if release else None,
        "holdout_regressions": test_regressions, "rounds": rounds, "lessons": learned,
    }
    _write_json(run_path / "holdout.json", {
        "baseline": [asdict(item) | {"passed": item.passed} for item in initial_test],
        "probed": [asdict(item) | {"passed": item.passed} for item in test],
    })
    if release:
        with TemporaryDirectory(dir=output) as staging:
            copy = Path(staging) / version
            shutil.copytree(candidate_root / version, copy, ignore=shutil.ignore_patterns("probe.json"))
            copy.rename(output / version)
        if use_experience:
            experience_root.mkdir(exist_ok=True)
            with TemporaryDirectory(dir=experience_root) as staging:
                temp = Path(staging) / "lessons.json"
                _write_json(temp, learned)
                temp.rename(experience_root / f"{run_id}.json")
    _write_json(run_path / "report.json", report)
    return report
