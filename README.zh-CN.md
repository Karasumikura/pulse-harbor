# PulseHarbor

[English](README.md) | 中文

PulseHarbor 是一个面向 **Hermes** 和 **OpenClaw** 这类机器人/自动化运行时的量化监控、预测和去重提醒脚本。

它适合 cron 风格的告警管道：高频运行，只有出现值得提醒的变化时才输出内容；没有有效变化时保持静默，不刷屏。

PulseHarbor 不是普通价格提醒器。它更像一个小型市场信号引擎，试图回答交易者真正关心的问题：**局面有没有发生值得行动的变化？** 它会把日内价格、日线技术背景、同业相对强弱、公司结构、SEC filings、新闻风险和预测状态记忆整合成可以直接转发到聊天窗口的简洁提醒。

## 为什么是 PulseHarbor

很多行情机器人要么太粗糙，要么太吵：价格动一下就提醒，几分钟后又重复同样的话，而且没有记住上一次提醒真正有用的上下文。

PulseHarbor 的思路不一样：

- 它既是监控器，也是短窗口预测器。
- 它把快信号和慢背景分开，让报价、K 线、新闻、公司数据各司其职。
- 它会记住之前的预测状态，没有实质变化就不重复提醒。
- 它输出的是可以直接给 Hermes、OpenClaw 和机器人桥接器发送的文字，而不是一堆原始指标。
- 它坚持免费 Key 优先，让个人也可以不用付费行情订阅搭出可用的告警系统。

## 信号引擎

PulseHarbor 会综合多层信息后再决定是否开口：

- **日内层**：报价质量、VWAP 偏离、15m 趋势、1h 趋势、日内高低点修复、短线支撑压力。
- **日线层**：3/5/20 日涨跌幅、日线支撑压力、ATR 分位、跳空缺口、成交量异常、相对大盘/板块强弱。
- **FMP 公司层**：行业/板块、同业篮子、流通股/流动性结构、报告日期、SEC filings、结构风险标签。
- **新闻/事件层**：Marketaux 新闻标题，加上 FMP filings 和公司背景因子。
- **预测层**：15m-1h 路径概率、主路径、验证条件、失效条件和置信度。
- **去重层**：语义状态 key、实质价格位移、冷却时间和预测历史评估。

最终目标是让提醒更安静、更有信息量，把注意力留给真正的新变化。

## 完全免费 Key 优先

这个项目的设计目标是使用 **完全免费的 API Key / 免费层 Key**。不需要付费行情订阅，也不要求付费 key。

支持的免费 key / 免费数据源：

- Finnhub 免费 API key：用于报价。
- Twelve Data 免费 API key：用于报价和 K 线。
- Financial Modeling Prep 免费 API key：用于公司背景层，包括 profile、同业、流通股/流动性、报告日期、SEC filings，以及免费套餐允许的事件数据。
- Alpha Vantage 免费 API key：用于日线 fallback。
- Marketaux 免费 API key：用于简单新闻上下文。
- Yahoo Finance：无需 API key 的兜底数据源。

免费 API 可能有延迟、限速、套餐限制或临时不可用。脚本会把这些数据源都当作可选项，某个源失败时自动尝试 fallback。

## 专为 Hermes / OpenClaw 设计

脚本只有在预测状态、关键价位、或实质价格位移发生有意义变化时才打印提醒文本。因此它很适合：

- Hermes cron job
- OpenClaw 定时任务
- QQ / Telegram / Discord 机器人转发
- 任何“stdout 非空才发送”的 cron 包装器

如果没有值得提醒的新变化，命令会静默退出。

## 功能

- 多报价源 fallback：Finnhub、Twelve Data、Yahoo Finance；FMP quote 只在免费套餐允许时作为补充。
- K 线 fallback：Twelve Data、Yahoo Finance、Alpha Vantage；FMP EOD 只在免费套餐允许时作为补充。
- 日线背景：3/5/20 日涨跌幅、支撑压力、ATR 分位、跳空缺口、成交量异常、相对 `SPY`、`QQQ`、`SMH` 强弱。
- FMP 公司背景层：行业/板块、同业篮子、流通股结构、报告日期、SEC filings、M&A/事件线索和结构风险标签。
- 预测层：15m-1h 路径概率、验证条件、失效条件。
- 去重提醒：预测状态、关键价位或价格位移没有实质变化时，不重复提醒。
- 本地 JSON 状态文件保存预测历史和后续评估数据。
- 可选 Marketaux 新闻因子，以及 FMP filings / 公司背景因子。
- Cron 原生静默模式：没有提醒时 stdout 为空，机器人集成更干净。
- 数据源感知 fallback：缺 key、限速、套餐限制、报价陈旧都会被温和处理。
- 可读性提醒卡片：每条提醒包含判断、动作倾向、上下文、概率、验证/失效价位和关键依据。

## 运行要求

- Python 3.10+
- 系统 PATH 中可用的 `curl`
- 你想启用的数据源对应的免费 API key

脚本只使用 Python 标准库。

## 设置

```bash
cp .env.example .env
python quant_alert_predictor.py --print-config
```

把你拥有的免费 key 填进 `.env`。只靠 Yahoo fallback 也能运行，但加入免费层 key 后，报价新鲜度、日线背景和新闻因子会更好。

## 用法

```bash
python quant_alert_predictor.py --symbol SNDK
python quant_alert_predictor.py --symbol AAPL --state-file ./state/aapl_state.json
python quant_alert_predictor.py --symbol NVDA --env-file /path/to/.env
```

没有需要发送的提醒时，命令不会输出任何内容。如果有输出，就把 stdout 交给 Hermes、OpenClaw、机器人桥接器或通知系统发送。

## 环境变量

支持的数据源变量：

```bash
FINNHUB_API_KEY=
TWELVE_DATA_API_KEY=
FMP_API_KEY=
ALPHA_VANTAGE_API_KEY=
MARKETAUX_API_KEY=
```

运行默认值：

```bash
QAP_SYMBOL=SNDK
QAP_ENV_PATH=./.env
QAP_STATE_PATH=./state/sndk_state.json
```

命令行参数优先级高于环境变量。

## Hermes / OpenClaw Cron 示例

每分钟运行一次，只转发非空输出：

```bash
* * * * * cd /opt/pulse-harbor && python3 quant_alert_predictor.py --symbol SNDK >> /tmp/qap_sndk.out 2>> /tmp/qap_sndk.err
```

在 Hermes 或 OpenClaw 中，可以把同样的命令作为定时任务；只有 stdout 非空时才发送消息。

## 后续计划

如果这个项目对你有帮助，欢迎 Star。PulseHarbor 后续会持续更新，接入更多有用、高质量、完全免费的 API。

## 注意

这是告警和预测辅助工具，不是投资建议。免费数据 API 可能延迟、不完整、被限速或受套餐限制。脚本会把不可用的可选数据源作为 fallback 处理，并尽量继续运行。
