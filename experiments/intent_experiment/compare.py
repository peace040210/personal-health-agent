import argparse
import gc
import hashlib
import json
import math
import statistics
import time
from datetime import date
from pathlib import Path

from health_agent.domain import Context, Metric, Status
from health_agent.mlx_intent import MlxIntent
from health_agent.model import Ollama
from health_agent.router import INTENT_CHOICES, INTENT_SYSTEM, Router
from .dataset import DATA, payload, read_rows
from .train import ADAPTER, ARTIFACTS


ROUTES = (
    ("昨天走了多少步", None, (), ("analyze",)),
    ("对比上周和上上周的睡眠时长", None, (), ("analyze",)),
    ("解释步数缺测日怎么处理", None, (), ("explain",)),
    ("根据本周睡眠记录，给我制定一个改善计划", None, (), ("plan",)),
    ("复盘本周的步数目标", None, (), ("review_goal",)),
    ("把我的每日步数目标设置为 9000 步", None, (), ("set_goal",)),
    ("比较两周的睡眠时长，另外复盘步数目标", None, (), ("analyze", "review_goal")),
    ("只比较本周和上周的睡眠时长，别解释", None, (), ("analyze",)),
    ("不用看步数，只查上周睡眠", None, (), ("analyze",)),
    ("如果上周步数没达标，再把步数目标改成每天8000步", None, (), ()),
    ("我已经查过了上周睡眠数据", None, (), ()),
    ("下周再设定睡眠目标到8小时", None, (), ()),
    ("可以把睡眠目标设置为每天9小时吗", None, (), ()),
    ("给我用药来改善睡眠", None, (), ()),
    ("我想和你说睡眠的事", None, (), ()),
    ("报告里那一小截呢", "sleep_hours", (("我询问上周每日睡了多久", "前半周的睡眠时长已经列出。"),), ("analyze",)),
    ("给个能塞进午饭后的招", "steps", (("我想让每天步数更稳定", "你想试试容易坚持的时间安排吗？"),), ("plan",)),
    ("这比原定标准还差几成", "steps", (("想回顾上周的步数目标", "我们已有新记录。"),), ("review_goal",)),
    ("那段区间是什么含义", "sleep_hours", (("图里这个范围有点奇怪", "那是你的睡眠时长区间。"),), ("explain",)),
    ("按工作日合计一下", "steps", (("我上传了几周步数", "现在可以用这些记录。"),), ("analyze",)),
    ("之前那组先搁置", "sleep_hours", (("要复盘你的睡眠目标吗", "可以用最近一周记录。"),), ()),
    ("我现在就想拿点能在室内做的办法", "steps", (("天气不好，走路不好坚持", "我们可以找更方便的步行安排。"),), ("plan",)),
    ("这几个数字的含义呢", "sleep_hours", (("我注意到对比了两种睡眠指标", "这些数有不同的计算方法。"),), ("explain",)),
    ("这已经弄完咯", "steps", (("之前在比较这周步数", "你还需要处理吗？"),), ()),
)


def latency(rows):
    values = sorted(row["ms"] for row in rows)
    return {"median_ms": round(statistics.median(values), 1), "p95_ms": round(values[math.ceil(len(values) * 0.95) - 1], 1)}


def classification_metrics(predictions):
    labels = {}
    for label in INTENT_CHOICES:
        actual = sum(row["expected"] == label for row in predictions)
        predicted = sum(row["predicted"] == label for row in predictions)
        correct = sum(row["expected"] == row["predicted"] == label for row in predictions)
        labels[label] = {"support": actual, "correct": correct, "f1": round(2 * correct / (actual + predicted), 4)}
    return {
        "correct": sum(row["expected"] == row["predicted"] for row in predictions), "total": len(predictions),
        "macro_f1": round(statistics.mean(label["f1"] for label in labels.values()), 4),
        "invalid": sum(row["predicted"] is None for row in predictions),
        "by_label": labels, **latency(predictions),
    }


class CountingIntent:
    def __init__(self, model):
        self.model = model
        self.calls = 0

    def choose(self, system, payload, choices):
        self.calls += 1
        return self.model.choose(system, payload, choices)


def evaluate_routes(model):
    counter = CountingIntent(model) if model else None
    router = Router(counter)
    results = []
    for text, metric_name, recent, expected in ROUTES:
        metric = Metric(metric_name) if metric_name else None
        context = Context(metric, None, recent, {Metric.SLEEP: 8, Metric.STEPS: 8000})
        before = counter.calls if counter else 0
        start = time.perf_counter()
        route = router.plan(text, context, date(2024, 12, 15))
        elapsed = (time.perf_counter() - start) * 1000
        predicted = tuple(task.intent.value for task in route.tasks if task.status is Status.ACTIVE)
        results.append({"text": text, "expected": expected, "predicted": predicted,
                        "calls": counter.calls - before if counter else 0, "ms": round(elapsed, 3)})
    eligible = [row for row in results if row["calls"]]
    return {
        "correct": sum(row["expected"] == row["predicted"] for row in results), "total": len(results),
        "fallback_cases": len(eligible), "fallback_correct": sum(row["expected"] == row["predicted"] for row in eligible),
        "unexpected_actions": sum(bool(row["predicted"]) and not row["expected"] for row in results),
        **latency(results), "predictions": results,
    }


def evaluate_classifier(model, rows, label):
    # Warm the model on validation prompts, then use the same held-out ordering and greedy decoding.
    for row in read_rows("valid")[:3]:
        model.choose(INTENT_SYSTEM, payload(row), INTENT_CHOICES)
    predictions = []
    for row in rows:
        prompt = payload(row)
        start = time.perf_counter()
        prediction = model.choose(INTENT_SYSTEM, prompt, INTENT_CHOICES)
        predictions.append({"text": row["text"], "recent": row["recent"], "expected": row["choice"],
                            "predicted": prediction, "ms": round((time.perf_counter() - start) * 1000, 3)})
    result = classification_metrics(predictions)
    print(label, {key: value for key, value in result.items() if key != "by_label"}, flush=True)
    return {**result, "predictions": predictions}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--ollama", default="qwen2.5:1.5b")
    parser.add_argument("--closed-choice", action="store_true", help="score valid labels with the same constraint for both Qwen3 checkpoints")
    args = parser.parse_args()
    rows = read_rows("test")
    results = {
        "system_prompt": INTENT_SYSTEM,
        "test_sha256": hashlib.sha256((DATA / "test_rows.jsonl").read_bytes()).hexdigest(),
        "adapter_sha256": hashlib.sha256((ADAPTER / "adapters.safetensors").read_bytes()).hexdigest(),
        "classifiers": {}, "routing": {"rules_only": evaluate_routes(None)},
    }
    factories = {
        "qwen3_base": lambda: MlxIntent(args.base, closed_choice=args.closed_choice),
        "qwen3_lora": lambda: MlxIntent(args.base, ADAPTER, closed_choice=args.closed_choice),
    }
    if not args.closed_choice:
        factories["ollama_qwen2_5"] = lambda: Ollama(args.ollama)
    output = ARTIFACTS / ("comparison_closed_choice.json" if args.closed_choice else "comparison.json")
    for label, factory in factories.items():
        model = factory()
        results["classifiers"][label] = evaluate_classifier(model, rows, label)
        results["routing"][label] = evaluate_routes(model)
        print(label, "routing", {key: value for key, value in results["routing"][label].items()
                                 if key != "predictions"}, flush=True)
        del model
        gc.collect()
        output.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
