# 意图识别实验

这个实验测的是睡眠和步数助手的本地意图路由。路由先拆分本轮请求，判定指标、时间范围、否定与延后状态。规则没有命中而且已经确定指标时，模型从 `analyze`、`explain`、`plan`、`review_goal` 和 `unknown` 中选一个。写目标由本地规则解析数值、单位和状态。DeepSeek 在任务确认后选择 Agent 协作路径，再改写各 Agent 的子问题。

## 数据和训练

- 训练 270 条，验证 25 条，留出测试 60 条。留出集五类各 12 条，其中 10 条需要前文。训练集包含 13 个从论文 S1 改写的中文意图种子及其变体，共 52 条。其余样本由 AI 构造和标注，尚未经过独立人工复核。论文分析的 1,370 条原始用户提问没有纳入实验。 [Nature 论文补充材料 S1](https://static-content.springer.com/esm/art%3A10.1038%2Fs41586-026-11161-8/MediaObjects/41586_2026_11161_MOESM1_ESM.pdf#page=3)
- 训练变体按种子分组，验证和测试单独编写。`data/*_rows.jsonl` 保留样本来源、上下文和标签。所有输入为合成问题，Google PHA 的日汇总单独用于工作流联调。
- 在 Apple M4 16 GB 上用 MLX 对 Qwen3-0.6B 的 BF16 基础权重做 LoRA SFT，冻结基础权重，在最后 8 层的注意力 Q/V 投影使用 rank 8、scale 16，训练 96 步，batch 4，学习率 `1e-4`，只计算回复 token 的损失。未使用 QLoRA、DPO 或强化学习。

## 评测协议

分类实验向三个本机模型提供同一条提示词、五个标签和相同的用例。Qwen3 的微调前后都通过同一个 MLX 解码及 JSON 校验流程。Ollama Qwen2.5-1.5B 使用服务端 JSON Schema 约束，属于另一条运行基线。分类 P95 为本机预热后的单条调用时延。

路由实验使用另外 24 条输入，比较本地路由器生成的有效任务序列。只有规则未能决定的用例调用模型。用例覆盖复合请求、省略和指代、延后、否定、任务超范围和目标操作。用例原本无需动作却生成任务时，计一次多派。这个实验测到 Router 输出为止，DeepSeek 联调单独检查。

首轮提示词缺少明确的 JSON 输出契约，基础模型在 60 条上全部解析失败。修正后加入五类定义和 JSON 示例，重新训练适配器，并用相同留出集复测。留出集文本和标签的 SHA-256 始终为 `60aa05989ae02f072cb1ff1454224d3ebf13db61e5d9b774856788b330f8af56`。首轮记录见 [试跑结果](artifacts_pilot_v1/comparison.json)。

## 实测结果

| 配置 | 五类准确 | Macro-F1 | 无效输出 | 本地分类 P95 | 有效任务全对 | 多派 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 纯规则 |  |  |  |  | 17/24 | 0 |
| Qwen3-0.6B 基础 | 9/60 | 0.229 | 41/60 | 240 ms | 17/24 | 0 |
| Qwen3-0.6B + LoRA | 29/60 | 0.397 | 0/60 | 222 ms | 20/24 | 1 |
| Ollama Qwen2.5-1.5B | 36/60 | 0.565 | 0/60 | 208 ms | 22/24 | 1 |

实际路由的 24 条用例中，有 10 条触发回退。基础模型答对其中 3 条，LoRA 答对 6 条，Ollama 答对 8 条。LoRA 能接住“给个能塞进午饭后的招”，却把有步数上下文的“按工作日合计一下”派给教练。LoRA 和 Ollama 都把“我想和你说睡眠的事”这一无明确动作的请求派发了出去。LoRA 训练用时约 98 秒，MLX 报告峰值内存约 2.97 GB。 [逐条评测结果](artifacts/comparison.json) · [训练记录](artifacts/training.json)

### 相同输出约束下的复测

在相同提示词下给 Qwen3 的两个版本提供共同的 JSON 前缀，在五个标签分叉的 token 上比较分数。两版都从五个合法标签里选。这轮诊断使用上一轮的相同输入和标签，用于排除格式错误。

| 配置 | 五类准确 | Macro-F1 | 有效任务全对 | 多派 |
| --- | ---: | ---: | ---: | ---: |
| Qwen3-0.6B 基础 + 限定五类 | 32/60 | 0.518 | 21/24 | 0 |
| Qwen3-0.6B LoRA + 限定五类 | 29/60 | 0.396 | 20/24 | 1 |

LoRA 的主要收益是 JSON 输出合规。统一约束后，这版适配器的分类分数和派发表现均低于同一基础模型，其中 `analyze` 在留出集只得 0/12，基础模型得 7/12。默认路由使用规则与澄清。要试用本机语义回退，可以在 DeepSeek 后端旁启用 Qwen3 基础模型的 `--intent-closed-choice`。离线对比的样本由 AI 标注，后续真实用户验证需要独立数据。 [限定五类逐条结果](artifacts/comparison_closed_choice.json)

在 Google PHA 的 365 天公开合成日汇总上验证了两种接法。本机意图模型根据前文将隐含请求判为 `plan`，DeepSeek 返回的工作流分派和子问题均通过本地校验，教练收到已确认的步数任务。 [基础模型联调](artifacts/workflow_smoke_closed_choice.json) · [LoRA 联调](artifacts/workflow_smoke.json)

## 复现

在项目根目录运行。`MLX_PYTHON` 指向装有 `mlx-lm` 的 Python，`QWEN3_BASE` 指向本地 Qwen3-0.6B 权重，`ECHOMIND_ENV` 指向 EchoMind 的 `.env`。训练要求 `artifacts/adapter` 没有已有权重，评测使用当前的适配器和本机 Ollama `qwen2.5:1.5b`。

```sh
"$MLX_PYTHON" -m experiments.intent_experiment.train --base "$QWEN3_BASE"
"$MLX_PYTHON" -m experiments.intent_experiment.compare --base "$QWEN3_BASE"
"$MLX_PYTHON" -m experiments.intent_experiment.compare --closed-choice --base "$QWEN3_BASE"
python3 -m health_agent --deepseek-env "$ECHOMIND_ENV" --trace pha-demo
"$MLX_PYTHON" -m health_agent --deepseek-env "$ECHOMIND_ENV" \
  --intent-base "$QWEN3_BASE" --intent-closed-choice --trace pha-demo
"$MLX_PYTHON" -m health_agent --deepseek-env "$ECHOMIND_ENV" \
  --intent-base "$QWEN3_BASE" --intent-adapter experiments/intent_experiment/artifacts/adapter \
  --trace pha-demo
```

## 云端 4B 实验

云端实验使用 Qwen3-4B-Instruct-2507 的 BF16 权重和单张 NVIDIA GPU。训练前后都在同一模型、同一提示词下对五个标签打分。LoRA 训练损失只比较五个标签的输出分数，校验集 Macro-F1 用于选取最佳 epoch，同分时优先选择拒识召回更高的版本。

扩充数据由模板和不同指标、时间窗口、既有目标及行动约束组合生成，共 1,515 条训练、160 条验证和 140 条新场景测试。训练集包含 730 条带前文的输入和 147 个模板或种子族。相同短回复会配上不同前文，以检验模型能否区分查询、解释、计划和复盘。完整的模板和场景在生成前分配到各个集合。扩充数据属于合成样本，旧 60 条留出集和 24 条路由用例继续作为对照。

在带 PyTorch CUDA 的 Linux 容器上，从项目根目录运行。`MODEL_PATH` 指向已经下载的官方权重目录，`OUTPUT_PATH` 为新的结果目录。建议参考仓库根目录的 `cloud_qwen4b.ipynb` 准备训练环境。

```sh
python -m experiments.intent_experiment.cloud_data
python -m experiments.intent_experiment.train_cuda --model "$MODEL_PATH" --out "$OUTPUT_PATH"
```

### 云端实测

在并行智算云的单张 RTX 4090 上完成两轮 BF16 LoRA 训练。第 1 轮验证集 Macro-F1 为 0.901，第 2 轮为 0.894，最终选择第 1 轮。加载权重、训练与评测共用时约 130 秒，PyTorch 记录的峰值显存约 10.22 GB。

| 用例 | 微调前 | 微调后 |
| --- | ---: | ---: |
| 原分类集 | 55/60 | 57/60 |
| 新合成场景 | 125/140 | 135/140 |
| 有效任务派发全对 | 22/24 | 23/24 |
| 无需动作却派发任务 | 1 | 1 |

原分类集的目标复盘从 9/12 提升到 12/12，查询从 12/12 降至 9/12。路由用例“这比原定标准还差几成”由误派查询改为正确复盘。“我想和你说睡眠的事”仍触发派发，从微调前的查询变成微调后的解释。新场景成绩适合比较这轮设置，尚需独立人工复核的数据验证真实请求。 [逐条评测与实验设置](artifacts_cloud_4b/comparison.json) · [验证集选点](artifacts_cloud_4b/validation.json) · [PEFT 适配器配置](artifacts_cloud_4b/adapter/adapter_config.json)
