# Google PHA 官方合成样例

这四份 CSV 来自 Google LLC 的 PHA 公开实现，保留了原文件内容和日期。它们描述同一个虚构人物，适合演示和测试。[上游数据说明](https://github.com/Google-Health/consumer-health-research/blob/3e9d1e4877a54cf81ce8ef34085006952dd19601/personal_health_agent/data/README.md)

| 文件 | 记录数 | 当前用途 |
| --- | ---: | --- |
| `summary.csv` | 365 | 导入每日睡眠时长和步数 |
| `activities.csv` | 403 | 保留运动事件样例 |
| `profile.csv` | 1 | 保留虚构人物样例 |
| `population_percentiles.csv` | 1,400 | 保留合成群体统计样例 |

日汇总日期为 2023-12-17 至 2024-12-15。导入时将 `datetime` 转为日期、`sleep_minutes` 除以 60，并把 `steps` 作为整数存储。其余列保留在原始 CSV 中。运行 `python3 -m health_agent --trace pha-demo` 会以 2024-12-15 为演示基准日。

来源为 [`personal_health_agent/data/sample`](https://github.com/Google-Health/consumer-health-research/tree/3e9d1e4877a54cf81ce8ef34085006952dd19601/personal_health_agent/data/sample)。Copyright 2025 Google LLC，Apache License 2.0，见同目录 `LICENSE.txt`。
