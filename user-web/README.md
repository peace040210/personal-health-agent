# 栖步 · 日常健康助手

PersonalHealthAgent 的用户体验版，包含健康概览、样例问答和七日计划。页面在浏览器内处理提问并计算步数、睡眠和配对统计。相对日期以样例最后一天为参照，单晚睡眠按入睡日查询，简短追问会沿用上一轮的指标或时间范围。计划与打卡保存在浏览器。FastAPI 演示工作台的用法见上级项目说明。

## 本地预览与检查

```sh
python3 -m http.server 8875 --bind 127.0.0.1 --directory dist
node --test tests/health.test.mjs
```

在浏览器打开 http://127.0.0.1:8875 。使用同一个浏览器访问同一地址可以恢复已保存计划。

从上级目录启动 `python -m health_agent.web` 后，可在 `/qibu/#chat` 使用连接后端的版本。它通过 `/api/qibu/chat` 提问，调用 `/api/qibu/actions` 确认、打卡和移除行动。配置模型后由模型提取意图，后端验证日期、证据和写入参数。

## 样例数据

[Google PHA 合成数据](https://github.com/Google-Health/consumer-health-research/tree/3e9d1e4877a54cf81ce8ef34085006952dd19601/personal_health_agent/data/sample)提供日记录，Copyright 2025 Google LLC。部署保留了 [Apache-2.0 许可](dist/data-LICENSE.txt)。从原始样例更新浏览器数据的命令为 `python3 scripts/export-sample.py ../examples/google_pha/summary.csv dist/data.json`。

首页照片由内置 ImageGen 生成，保存在 [morning-trail.jpg](dist/morning-trail.jpg)。生成提示词见 [图片说明](image-prompt.md)。
