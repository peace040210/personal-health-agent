# 栖步验收与内测

## 自动验收

测试使用合成记录和独立的浏览器会话。启动临时 FastAPI 实例，使用单独的 SQLite 目录。将 `QIBU_MODEL_ENV` 设为本机已有的 DeepSeek 配置文件路径。

```sh
python -m pip install ".[web,test,mcp]"
python -m health_agent.web --port 8876 --qibu-home \
  --deepseek-env "$QIBU_MODEL_ENV" \
  --qibu-data-dir "$(mktemp -d)"
```

在另一个终端从项目根目录运行。

```sh
npm install --no-save --no-package-lock playwright
npx playwright install chromium
node scripts/qibu_acceptance.mjs
python -m unittest discover -s tests
node --test user-web/tests/health.test.mjs
```

浏览器检查覆盖缺项追问、修改日期和计划周期、确认和打卡、刷新后续聊、无记录、跨会话记忆，以及断连、503 和提交成功后响应丢失。Python 回归会额外模拟模型无响应，核对降级后仍遵守查询日期。

## 人工试用

让 3–5 名试用者在独立浏览器资料中操作，每人约 10 分钟。发给试用者下表的任务，让他们按自己的习惯表达。使用合成记录，反馈中仅记录模拟输入。

| 任务 | 通过条件 |
| --- | --- |
| 查昨晚睡得怎么样，刷新后追问深睡，再改查前一晚 | 日期可核对，追问沿用原日期，改口更新日期 |
| 先定一周散步，再改成今天午休，最后确认并打卡 | 追问缺少的时长，改口后只有单日提议，确认后写入，刷新后仍能看到 |
| 询问一个家庭成员的睡眠问题，随后补年龄 | 追问年龄，补齐后给出有来源的回答 |
| 查合成样例之外的日期 | 如实报告缺记录，并显示查询日期 |
| 表达固定工作日午休限制，再从新会话安排午休 | 确认后才能复用，移除后停止复用 |
| 由主持人短暂停止测试服务，再恢复并重试 | 页面给出可读错误，重试后避免重复行动，旧会话可恢复 |

每条反馈记录原话、出现问题前的上一轮、页面实际结果、预期结果和是否能复现。优先处理日期错配、未经确认保存、重复写入和会话串读，此类问题修复后重跑浏览器验收。
