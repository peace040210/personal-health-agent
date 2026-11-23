import hashlib
import json
from pathlib import Path

from health_agent.router import INTENT_CHOICES, INTENT_SYSTEM, _retrieve


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SYSTEM = INTENT_SYSTEM + "\n可选 JSON 值：" + "、".join(INTENT_CHOICES)
S1 = "Nature Supplementary Table S1, Chinese task adaptation"

PAPER_SEEDS = {
    "analyze": ["上个月哪天睡眠时间最长", "上周每天大概走多少步", "这一周和上一周的睡眠时长有何变化", "给我一份最近的每日活动概况"],
    "explain": ["什么是睡眠时长", "步数和运动时长的概念有什么区别", "怎样理解睡眠时长变化的含义"],
    "plan": ["我怎样改善自己的睡眠习惯", "帮我建立一个更有规律的步行计划", "外面下雨了，帮我调整今天的步行安排"],
    "review_goal": ["按最近的训练记录，我距离步行目标还有多远", "结合最近记录判断我的活动计划是否按进度推进"],
    "unknown": ["喉咙痒并且吞咽困难，可能是怎么回事"],
}

TRAIN = {
    "analyze": ["近十四天有效的睡眠记录有几天", "把工作日的步数加起来", "上周和上上周平均每天走多少步", "按时间看看睡眠时长的波动", "最近一周步数最低的那天是多少", "把昨晚的睡眠记录调出来", "能给我近十天步数逐日概况吗", "本周睡眠时长大致在什么范围", "看看运动日与其他日的睡眠时长差值", "这几天步数整体往上还是往下"],
    "explain": ["为什么没记录的天不能算成零步", "睡眠时长的均值和中位数有何区别", "何谓步数的有效记录天数", "报表里说睡眠覆盖不足是什么意思", "看步数走势时为什么要留意周末与工作日", "两个睡眠窗口缺失天数不同会怎样影响解读", "给我讲讲怎样读睡眠线性趋势", "为什么我的步数日均值和总数是两种指标", "睡眠总时长和卧床时长如何区分", "什么叫只在有记录日计算步数均值"],
    "plan": ["怎样安排一周的散步时间更方便坚持", "最近睡得不规律，帮我设计个下班后的习惯", "我工作日太忙，给一个周末步行方案", "怎么逐渐养成日常走路的习惯", "给我几种不用增加时间也能多走的做法", "下周有三天出差，帮我改一下每天走路的安排", "我想连续两周养成记录睡眠的习惯，怎么做", "想让自己的睡眠更规律，给个容易做到的起点", "帮我把步行习惯拆成几个轻量步骤", "我的时间很零碎，散步可以怎样规划"],
    "review_goal": ["这周睡眠目标完成得如何", "回顾上周步数达标的比例", "按我已存的每日步数目标，有几天完成了", "我离连续一周睡够的目标还有几天", "复查近十天的步数目标执行情况", "结合新记录小结睡眠计划的完成情况", "昨天步数是否达到了之前的门槛", "按已有睡眠目标，列出本周达标天数", "之前设好了步数目标，最近进展怎样", "上周睡眠目标有多少天没完成"],
    "unknown": ["帮我设置每天八小时的睡眠目标", "不用计算昨天的步数", "上周的睡眠我已经查过了", "下周再一起复盘这次走路目标", "如果有空再给我做睡眠计划", "我只是在提到以前的步数目标", "帮我查一下运动手表的保修期限", "睡眠记录能帮我诊断是什么疾病吗", "不用解释睡眠时长的统计原理", "把我的每日步数目标直接改成九千", "我听说有人在比较步数，随口一提", "睡眠那个先放着吧"],
}

TRAIN_CONTEXT = [
    ("analyze", "今天呢", "我想看每天的睡眠时长", "你想从哪一天开始看？"),
    ("analyze", "两周放一起看看", "目前显示本周步数", "要加上上周来比较吗？"),
    ("explain", "那个分母为何这样取", "睡眠平均值只用了有记录的日子", "这个统计有一个分母。"),
    ("explain", "那个零该怎么看", "我的步数记录里有一天是零", "你想看数值含义吗？"),
    ("plan", "给个能塞进通勤里的做法", "我想多走路，但工作忙", "有什么时间方便活动？"),
    ("plan", "让它轻松一点，工作日能用", "我想建立规律睡眠习惯", "我们在设计你的行动方案。"),
    ("review_goal", "回顾一下执行进展", "我之前设过每日步数目标", "可以按这个目标复盘。"),
    ("review_goal", "这几天都过线了吗", "我的睡眠时长目标是每天七小时", "目标已经保存。"),
    ("unknown", "已经看过了", "你想看看上周的睡眠吗", "需要我计算吗？"),
    ("unknown", "改成一万", "你的当前步数目标是八千", "是否要另设一个目标？"),
]

VALID = {
    "analyze": ["求一下近五日的步数累计", "本周的睡眠时长按天排列一下", "昨晚合计睡多久", "比较最近两组记录的日均步数", "汇报昨天前一天的睡眠总量"],
    "explain": ["睡眠图中周趋势代表什么", "什么叫步数基线", "如何解读缺测情况下的睡眠差别", "帮我说清步数均值采用的分母", "解读一下睡眠线条上升的含义"],
    "plan": ["帮我把午休后的散步安排得有规律", "这周轮班，怎么做一个可行睡眠安排", "给我一个室内走动的备选方案", "怎样安排下周晚间减少拖延入睡", "能给我一个逐步增加日常活动的方案吗"],
    "review_goal": ["按早先的步数目标验收这七天", "把新睡眠记录放到原目标下看完成度", "目前走路目标已完成多少", "过去这一周睡眠指标过线几回", "帮我盘点已经承诺的步行目标进度"],
    "unknown": ["别再核算最近步数了", "条件满足以后才问睡眠怎么样", "我已经做好睡眠计划了", "我想直接改掉原来的步数目标", "步数记录能替我开药吗"],
}

TEST = {
    "analyze": ["最近三天的睡眠时长中位数呢", "周末的总步数摊到两天是多少", "睡眠日志里哪一天最短", "已上传的步数按周汇总给我", "最近的睡眠记录缺了几晚", "我这个星期走的总量报给我", "比一下两个周末的走路量", "前天那晚合计休息几小时", "把近六日睡眠数字列一下", "给我睡眠时长的最小最大值"],
    "explain": ["步数走平应该怎么理解", "哪种缺测会让睡眠均值偏高", "这里的睡眠窗口具体是啥概念", "想理解步数折线的斜率", "睡眠观测日和自然日有什么不同", "怎样读懂步数取样覆盖率", "记录了零步和缺少一天到底有何不同", "请描述睡眠累计值和日均值的关系", "为什么两周步数均值接近但总量不同", "睡眠记录中断那行应如何看待"],
    "plan": ["我想把走路安排进午饭后，有什么可坚持的办法", "来一个适合经常加班的人坚持的睡眠行动方案", "天气热想改到早晨走，有啥合理的安排", "想先从很短的散步开始，你建议怎么排", "我想逐周把睡眠作息稳定下来，能给个行动清单吗", "如果只考虑有空的工作日，怎样设计步行习惯", "每天下班晚，怎么做一个容易遵守的睡眠准备流程", "出门不便，帮我找在家活动的替代方案", "刚开始记录步数，怎样慢慢养成习惯", "有什么容易接受的一周睡眠调整安排"],
    "review_goal": ["已定的步行标准，这几天兑现得如何", "本周睡眠计划的执行成效可以复核吗", "对照前几天定的步数门槛，新数据有多少合格日", "这一周睡眠目标完成比例怎么样", "原先那个行走目标还有多少差距", "请按现有目标检查近期步数执行度", "能不能验收一下每日睡够这个目标", "按此前的睡眠指标盘点一下过线的天数", "用最新两周步数看我走路目标的完成度", "昨天我的睡眠达到已设阈值了吗"],
    "unknown": ["今天不用核查走路量", "什么时候再讨论睡眠目标都行，今天先不做", "我只是说朋友睡了六个小时", "请将我的睡眠指标写成每天七小时目标", "步数先别算，等我上传新数据再说", "我对昨晚的睡眠已经完成检查", "想用步数猜一下手表的购买价格", "我刚睡醒喉咙不适，判断是什么病", "睡眠规划目前不用调整", "要是下个月加班再来做步行计划"],
}

TEST_CONTEXT = [
    ("analyze", "把另一段也放进来", "我在对比前一周和这一周的睡眠时长", "目前只有前一周的数据。"),
    ("analyze", "只要后半段的合计", "我上传了两周的步数", "需要怎样统计？"),
    ("explain", "怎么会看起来反过来", "我这周步数总量更高，日均却更低", "可以解释两种统计口径。"),
    ("explain", "说清这数代表的意思", "结果里出现睡眠记录覆盖率", "你想了解它的计算口径吗？"),
    ("plan", "想办法换一个在家能坚持的", "户外步行计划这周很难实行", "需要调整活动方案吗？"),
    ("plan", "把它安排到早班之前吧", "我想固定一个步行时段", "你平时什么时间方便？"),
    ("review_goal", "帮我看看兑现了几成", "我想回顾之前约定的睡眠目标", "有最新数据可用。"),
    ("review_goal", "那我赶上预定进度了吗", "目标是这周连续三天达步数门槛", "已经导入这周的活动数据。"),
    ("unknown", "这回先不做了", "下周睡眠计划要我帮你制定吗", "你需要时我可以安排。"),
    ("unknown", "直接把数字录进去", "现在的每日步数目标还是八千吗", "之前的目标是八千。"),
]


def payload(row):
    return {"text": row["text"], "recent": row["recent"], "examples": _retrieve(row["text"])}


def messages(row):
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(payload(row), ensure_ascii=False)}]


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def read_rows(split):
    return [json.loads(line) for line in (DATA / f"{split}_rows.jsonl").read_text(encoding="utf-8").splitlines()]


def build():
    DATA.mkdir(parents=True, exist_ok=True)
    seen = {}
    for split, bank, contextual in (("train", TRAIN, TRAIN_CONTEXT), ("valid", VALID, ()), ("test", TEST, TEST_CONTEXT)):
        rows = []
        for label, questions in bank.items():
            seeds = [(question, "authored synthetic") for question in questions]
            if split == "train":
                seeds.extend((question, S1) for question in PAPER_SEEDS[label])
            for index, (question, source) in enumerate(seeds):
                family = f"{split}-{label}-{index:02d}"
                variants = (question, f"{question}？", f"嗯，{question}", f"我想问下，{question}") if split == "train" else (question,)
                rows.extend({"text": text, "choice": label, "recent": [], "family": family, "source": source} for text in variants)
        for index, (label, text, user, assistant) in enumerate(contextual):
            rows.append({"text": text, "choice": label, "recent": [[user, assistant]],
                         "family": f"{split}-context-{index:02d}", "source": "authored synthetic dialogue"})
        for row in rows:
            identity = row["text"].removesuffix("？")
            previous = seen.setdefault(identity, split)
            if previous != split:
                raise ValueError(f"Cross-split duplicate: {identity}")
        write_jsonl(DATA / f"{split}_rows.jsonl", rows)
        write_jsonl(DATA / f"{split}.jsonl", ({"messages": [*messages(row), {"role": "assistant",
                   "content": json.dumps({"choice": row["choice"]}, separators=(",", ":"))}]} for row in rows))
        print(split, len(rows), hashlib.sha256((DATA / f"{split}_rows.jsonl").read_bytes()).hexdigest())


if __name__ == "__main__":
    build()
