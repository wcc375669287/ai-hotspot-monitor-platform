# 24小时热点资讯监测平台

一个面向科技/AI领域的热点监测控制台，集成以下能力：
- 热点洞察：24小时热点识别、热度评分、趋势与标签统计
- 溯源分析：事件首发来源、证据链、跨渠道来源分布
- 选题推荐：基于热点自动生成可执行选题建议
- 订阅推送：关键词订阅、定时推送、受保护API触发

## 主要功能
- 默认抓取真实 RSS（可切换样例模式）
- 24 小时监测快照：`POST /monitor`，输出并持久化洞察 JSON
- 日报生成：`POST /generate`
- 订阅管理：创建/删除订阅
- 定时推送：`POST /run-push`（Web）与 `POST /api/run-push`（Token 保护）

## 本地启动
```bash
cd "/Users/shadowmr/Mac work-成川/AI应用/Codex/ai_hotspot_agent"
python3 src/web.py
```

启动后访问：
- 首页控制台：`/`
- 最新 Markdown 报告：`/report`
- 最新洞察快照 JSON：`/insights`

## 使用建议
1. 先点击“执行24小时监测”，生成热点洞察、溯源和选题推荐。
2. 再按需要“生成日报”或创建关键词订阅。
3. 生产环境使用定时器调用 `/api/run-push?only_due=1`，实现自动推送。

## 云端部署（Render）
1. 推送仓库到 GitHub。
2. Render -> New -> Blueprint，选择根目录 `render.yaml`。
3. 部署后在控制台创建订阅与监测任务。

## 定时触发推送（推荐）
每 15 分钟调用一次：
- URL: `https://你的域名/api/run-push?only_due=1`
- Method: `POST`
- Header: `X-Run-Token: <AI_AGENT_CRON_TOKEN>`

说明：
- `AI_AGENT_CRON_TOKEN` 为服务端环境变量（用于鉴权）。
- `only_due=1` 表示只推送已到发送时间的订阅。

## 目录
- 来源配置：`config/sources.json`
- 订阅配置：`data/subscriptions.json`（云端可映射到 `/var/data/subscriptions.json`）
- 每日报告：`output/radar_*.md`
- 订阅报告：`output/subscriptions/`
- 洞察快照：`output/insights/latest_snapshot.json`

## 当前来源覆盖
- 国际：TechCrunch / The Verge / Wired / MIT Tech Review / HN / GitHub / Product Hunt / OpenAI / DeepMind / Anthropic / arXiv / Reddit / X(RSSHub) / YouTube(RSSHub)
- 国内：36氪 / 钛媒体(RSSHub) / 虎嗅(RSSHub) / 机器之心 / 量子位 / 新智元 / InfoQ / 掘金 / V2EX / 知乎/微博/B站(RSSHub) / 公众号(RSSHub示例)
