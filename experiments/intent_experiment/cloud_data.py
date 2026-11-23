import hashlib
import json
from collections import Counter
from pathlib import Path

from .dataset import read_rows, write_jsonl
from health_agent.router import INTENT_CHOICES


ROOT = Path(__file__).resolve().parent
CLOUD_DATA = ROOT / "cloud_data"

# Each row describes a metric, reporting windows, a threshold, a statistic and a constraint.
# Windows, constraints and response templates are split before any combinations are generated.
SCENES = {
    "train": (
        ("步数", "最近十天", "本月第一周", "7500步", "中位数", "通勤要换乘两次"),
        ("步数", "这半个月", "上半个月", "9000步", "日均", "每天午休只有二十分钟"),
        ("步数", "这个周末", "上个周末", "6200步", "总数", "连续几天在下雨"),
        ("步数", "这四天", "前面的四天", "8300步", "最低值", "早上得送孩子上学"),
        ("步数", "刚导入的六天", "更早的六天", "7000步", "最高值", "上夜班后想尽量早回家"),
        ("步数", "最近两个工作日", "前两个工作日", "9500步", "有效样本数", "办公楼中午人比较少"),
        ("睡眠时长", "从周二到周五", "之前四天", "7小时", "均值", "工作结束得很晚"),
        ("睡眠时长", "这几晚", "月初那几晚", "7.5小时", "最短时长", "出差住在酒店"),
        ("睡眠时长", "过去九晚", "更早的九晚", "8小时", "最长时长", "工作日和休息日的安排不同"),
        ("睡眠时长", "周六周日", "上次休息日", "6.5小时", "四分位数", "明早要赶第一班车"),
        ("睡眠时长", "刚开始记的那三天", "当前三天", "7.2小时", "波动范围", "晚上有固定家务"),
        ("睡眠时长", "最近三个上班日", "前面一组上班日", "8.2小时", "有效记录数", "最近换了排班"),
    ),
    "valid": (
        ("步数", "最近八个自然日", "再之前八天", "8400步", "跨度", "想把走动安排在弹性的时段"),
        ("步数", "节后前五天", "节前最后五天", "5800步", "分位数", "只能在公司楼里活动"),
        ("睡眠时长", "周会前两天", "周会后两天", "7.8小时", "累计值", "要留出晚上带家人看电影的时间"),
        ("睡眠时长", "今年春节假期", "去年同一段", "8.4小时", "均方差", "平时会忘记给手表充电"),
    ),
    "test": (
        ("步数", "换手机后的前六天", "换手机前的六天", "6800步", "位次", "午饭后要在柜台值班"),
        ("步数", "这次驻场期间", "上一轮驻场", "10200步", "日平均", "这周只能在家里走动"),
        ("睡眠时长", "家人来住的五晚", "来之前五晚", "6.8小时", "分布区间", "需要把准备工作缩短到十分钟"),
        ("睡眠时长", "调休那一周", "再前面那周", "8.1小时", "有效天数", "周末想把作息过渡平稳一点"),
    ),
}

TRAIN = {
    "analyze": (
        "把{window}的{metric}按天排一下。", "算一算{metric}的{stat}，范围取{window}。",
        "想把{window}和{other}的{metric}放在一张表上。", "把{window}的{metric}从大到小排一下。",
        "{window}的{metric}整体往哪个方向走？", "只取{window}的{metric}，给我一个区间。",
        "哪一天拖低了{window}的{metric}？", "单列{window}的{metric}缺测日。",
        "请算出{window}与{other}的{metric}相差多少。", "把{window}的{metric}总量和覆盖天数列在一起。",
        "{window}的{metric}相邻两条差得最多是哪两天？", "我想看{window}的{metric}按日期累计到哪儿了。",
    ),
    "explain": (
        "在{window}的{metric}统计里，{stat}这一栏是什么意思？", "{window}的{metric}为什么把没有采样的日子留空？",
        "{window}的{metric}有一行被标成无效，这个标记代表什么？", "怎么读{window}的{metric}和{other}重叠的那块图？",
        "为什么{window}的{metric}总数会受记录天数影响？", "为什么报告里{window}的{metric}和{other}口径不同？",
        "报表已经有{stat}了，怎么理解它对{window}的{metric}的意义？", "请解读{window}的{metric}统计对样本不足的提示。",
        "{window}的{metric}曲线有断点，这种图怎么读？", "报告里说只算有效天，这和{window}的{metric}有什么关系？",
        "{window}的{metric}写着{target}，怎么看这个单位？", "{window}的{metric}有一个灰色区间，灰色说明什么？",
    ),
    "plan": (
        "{constraint}。给个改善{metric}的安排，先按{window}试行。", "想把{metric}变得更规律，按{window}帮我排个日程。",
        "基于{window}的{metric}选一个我明天可以做的动作。", "{metric}想逐步接近{target}，{constraint}，从哪里开始？",
        "{constraint}。帮我把调整{metric}的计划精简一下，先试到{window}结束。", "参考{other}的{metric}，给{window}安排一个低门槛做法。",
        "我希望改善{metric}，给一个可以在{window}跟踪的行动清单。", "{constraint}。想在{window}改善{metric}，给两个可选方案。",
        "为了把{metric}安排得更稳定，{window}可以养成什么小习惯？", "读了{window}的{metric}记录后，请给一条适合我当前约束的建议。{constraint}。",
        "想在{window}调整每天的{metric}记录习惯，怎么开始更容易？", "{constraint}，按{window}给一套不太费时间的{metric}改善安排。",
    ),
    "review_goal": (
        "用{window}的{metric}核对已经保存的{target}目标。", "之前定好{metric}{target}，{window}过关了几天？",
        "按原有的目标检查{window}的{metric}，离约定值还差多少？", "{window}的{metric}有多少天符合我当时设的{target}？",
        "之前约定{target}的{metric}门槛，用{window}的新记录验收。", "帮我回顾{window}按自定的{metric}{target}目标执行得怎样。",
        "{window}和{other}各有几天完成了我保存的{metric}目标？", "用{window}的{metric}算一下约定{target}的完成比例。",
        "把{window}的{metric}贴着旧目标{target}看，有哪几天没有到线？", "新导入{window}的{metric}了，帮我验一下原来的目标承诺。",
        "目标是以前录进去的{metric}{target}，我在{window}做到了吗？", "{window}的{metric}按之前约定标准完成了没？",
    ),
    "unknown": (
        "{window}的{metric}先不算，数据还没同步完。", "我只是提一下{window}的{metric}，暂时不需要处理。",
        "等我导出{window}的{metric}之后再来问。", "{window}的{metric}我刚才已经核对过。",
        "如果{window}的{metric}没达到{target}，以后有空再聊。", "把每日{metric}目标设成{target}，直接保存。",
        "{window}的{metric}那张图不用解读，我已经明白了。", "我朋友说他会看{window}的{metric}，我随口一提。",
        "{window}的{metric}先留在报告里，等我发下一条再处理。", "我是想问记录{window}的{metric}的手表怎么申请退货。",
        "本来想按{window}的{metric}做调整，今天取消吧。", "如果将来要复盘{window}的{metric}，到时我会再告诉你。",
    ),
}

VALID = {
    "analyze": ("{window}的{metric}合起来有多少？", "统计一下{window}的{metric}有几天能用。", "把{window}的{metric}做成逐日清单。", "{window}和{other}的{metric}哪组更大？"),
    "explain": ("{window}的{metric}这一项为何叫{stat}？", "缺了记录怎么看{window}的{metric}对照图？", "{window}的{metric}有分组权重，这个概念怎么理解？", "{window}的{metric}两种单位可以怎样换算？"),
    "plan": ("{constraint}。我想调整{metric}，给一个{window}能用的新办法。", "{window}该怎么帮助自己保持{metric}的习惯？", "用{window}的{metric}记录做一份下阶段行动建议。", "把{metric}的安排改得轻松一点，{constraint}。先试{window}。"),
    "review_goal": ("{window}已有{metric}，按登记的{target}做一次回顾。", "{window}的{metric}距离已保存的门槛有多远？", "对着原标准{target}看看{window}的{metric}达到几回。", "我先前设了{metric}{target}，用{window}验一验这个约定。"),
    "unknown": ("{window}的{metric}下个月有需要再处理。", "{window}的{metric}就算了，留给我自己核算。", "用{target}覆盖之前保存的{metric}目标。", "说起{window}的{metric}，我其实在等软件导出链接。"),
}

TEST = {
    "analyze": ("{metric}在{window}这段按记录日摊下来是多少？", "想知道{window}的{metric}有没有比{other}高。", "{window}的{metric}哪一条最低，麻烦帮我找出来。"),
    "explain": ("{window}的{metric}不同颜色分别说明了什么？", "那张{window}的{metric}图，把缺失和零放在不同位置是何意？", "{window}的{metric}怎么同时有{stat}和原始值？这两项怎么区分？"),
    "plan": ("{constraint}。想在{window}调整{metric}，给个能做到的第一步。", "看了{window}的{metric}曲线，接下来该怎么安排更易坚持？", "{window}的{metric}习惯太难保持，帮我想个更轻便的做法。"),
    "review_goal": ("我之前承诺{metric}做到{target}，{window}兑现了几成？", "以前存的那条{metric}要求，算上{window}这次完成得如何？", "请用{window}的{metric}给已登记的{target}门槛做个合格率。"),
    "unknown": ("{window}的{metric}照旧放着，今天没想问什么。", "我在和同事提到{window}的{metric}，这个不用你接手。", "请把{metric}的每日目标更新成{target}。"),
}

CONTEXT = {
    "train": (
        "那就按这个来吧", "嗯，就弄这一块", "只要刚才说的那个", "从那一段开始吧",
        "这项可以接着做", "现在做一下你提到的事", "就用刚才说的这一项", "行，可以动手了",
        "只做这个就好", "按你说的那项", "就这一件事", "可以，那继续吧",
    ),
    "valid": ("好的，请接着刚才提议的做", "我确认就处理那件", "那我想先要这部分", "嗯，上一句那个帮我弄一下"),
    "test": ("刚刚问到的那件，可以做了", "没有别的了，照那个办", "你说的这项我现在就需要", "对，按前面那条就可以"),
}

ABSTAIN = {
    "train": ("先不做了", "等我说了再弄", "不用继续", "取消刚才这件事", "明天有空再看", "我已弄完了",
              "这个先搁置", "我现在只是随口说说", "如果有需要再问", "暂时别答这部分", "这个先跳过", "之后我自己处理"),
    "valid": ("不用按刚才那个了", "下一次才做这项", "之前已经搞定", "这会先别动那一块"),
    "test": ("这一项我这会儿用不着", "等我明确了再开这个任务", "先把你那条建议收回", "我刚完成了，直接过吧"),
}


def rows_for(split):
    bank = {"train": TRAIN, "valid": VALID, "test": TEST}[split]
    for index, scene in enumerate(SCENES[split]):
        fields = dict(zip(("metric", "window", "other", "target", "stat", "constraint"), scene, strict=True))
        metric, window, other, target, stat, constraint = scene
        topics = {
            "analyze": (f"我导入了{window}和{other}的{metric}。", f"要我把两个时段的{stat}汇总并比较吗？"),
            "explain": (f"{window}的{metric}报告出来了，有一列{stat}。", f"需要我讲讲{stat}和报告里的缺测口径吗？"),
            "plan": (f"我在想怎么让{metric}更有规律。{constraint}。", f"要我按你的情况给一套{window}可做的安排吗？"),
            "review_goal": (f"我已经保存了每日{metric}{target}的目标。", f"要用{window}新上传的数据检查这个目标的完成情况吗？"),
        }
        for label, expressions in bank.items():
            for variant, expression in enumerate(expressions):
                yield {"text": expression.format(**fields), "recent": [], "choice": label,
                       "family": f"{split}-single-{label}-{variant}", "scenario": f"{split}-{index}",
                       "source": "synthetic scenario composition"}
        for variant, text in enumerate(CONTEXT[split]):
            for label in INTENT_CHOICES:
                topic = tuple(topics.values())[(index + variant) % len(topics)] if label == "unknown" else topics[label]
                yield {"text": ABSTAIN[split][variant] if label == "unknown" else text,
                       "recent": [list(topic)], "choice": label, "family": f"{split}-dialog-{variant}",
                       "scenario": f"{split}-{index}", "source": "synthetic matched dialogue"}


def prepare():
    CLOUD_DATA.mkdir(exist_ok=True)
    report = {}
    previous = {json.dumps([row["text"], row["recent"]], ensure_ascii=False): "original_test" for row in read_rows("test")}
    for split in ("train", "valid", "test"):
        rows = list(rows_for(split))
        if split == "train":
            seeds = {}
            for row in read_rows("train"):
                seeds.setdefault(row["family"], row)
            rows.extend(seeds.values())
        unique = []
        for row in rows:
            identity = json.dumps([row["text"], row["recent"]], ensure_ascii=False)
            old_split = previous.get(identity)
            if old_split and old_split != split:
                raise ValueError(f"Shared input between {old_split} and {split}: {identity}")
            if not old_split:
                unique.append(row)
                previous[identity] = split
        path = CLOUD_DATA / f"{split}.jsonl"
        write_jsonl(path, unique)
        report[split] = {
            "rows": len(unique), "dialogues": sum(bool(row["recent"]) for row in unique),
            "families": len({row["family"] for row in unique}), "labels": dict(Counter(row["choice"] for row in unique)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    (CLOUD_DATA / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(prepare(), ensure_ascii=False, indent=2))
