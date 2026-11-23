# Nature PHA 研究评测结果

`study-data.zip` 是论文发布的 Supplementary Data 原包，包含 DS、DE、HC 和完整 PHA 的 23 张 CSV 评分、评审排名和错误分类表。SHA-256 为 `2945e086af56c6922ed68bc1cafecd017efd777c93b2b970b081c06cf9a9fce8`。[论文](https://doi.org/10.1038/s41586-026-11161-8)，[官方评测数据](https://static-content.springer.com/esm/art%3A10.1038%2Fs41586-026-11161-8/MediaObjects/41586_2026_11161_MOESM4_ESM.zip)

原包仅限学术或非营利的非商业研究用途。使用时须引用论文，不得尝试重新识别任何人，也不得用于真实个人的临床决策。完整条件见压缩包内的 README。项目默认从 Git 忽略这个压缩包。

可从 `PHA/pha_task1_ranking.csv` 复核论文的多轮总体偏好。将 `preference_ranking` 等于 1 的记录按 `model_type` 计数，PHA 为 21/45，并行多 Agent 为 13/45，PHIA 单 Agent 为 11/45。这些是论文实验的结果。

项目运行和路由测试使用 `examples/google_pha/` 的官方合成样例。论文提到的 50 人物 WEAR-ME 评测原始记录未出现在这份结果包中，也未在论文所指 GitHub 仓库的公开数据目录或发布页找到。访问完整的 1,165 人 WEAR-ME 数据需要向研究方申请。[论文数据可用性说明](https://pmc.ncbi.nlm.nih.gov/articles/PMC13736220/)
