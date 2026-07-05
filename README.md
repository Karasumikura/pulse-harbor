# PulseHarbor

English | [中文](README.zh-CN.md)

PulseHarbor is a free-key market monitor and short-window predictor designed for bot and automation runtimes such as **Hermes** and **OpenClaw**.

It is built for cron-style alert pipelines: run it frequently, forward stdout only when it prints something, and stay silent when there is no meaningful change.

PulseHarbor is not a simple price alarm. It is a compact market-signal engine that tries to answer the question a human trader actually cares about: **did the situation meaningfully change?** It blends intraday price action, daily technical context, peer-relative strength, company structure, SEC filings, news risk, and forecast-state memory into concise alerts that are designed to be forwarded directly to chat.

## Why PulseHarbor

Most market bots are either too dumb or too loud. They trigger on a fixed percentage move, repeat the same warning every few minutes, and lose the context that made the first alert useful.

PulseHarbor is built differently:

- It watches the market like a monitor, but also behaves like a short-window forecaster.
- It separates fast signals from slow context, so quote APIs, candle APIs, news APIs, and company-data APIs each have a distinct job.
- It remembers previous forecast states and suppresses repeated alerts when nothing actionable changed.
- It outputs ready-to-send text for Hermes, OpenClaw, and bot bridges instead of dumping raw indicator values.
- It is intentionally free-key first, so a useful personal alert system can run without paid market-data subscriptions.

## Signal Engine

PulseHarbor combines several layers before it decides to speak:

- **Intraday layer**: quote quality, VWAP distance, 15m trend, 1h trend, day high/low recovery, short-term support and resistance.
- **Daily layer**: 3/5/20 day returns, daily support/resistance, ATR percentile, gap, volume anomaly, and relative strength vs major benchmarks.
- **FMP company layer**: sector, industry, peer basket, float/liquidity structure, report dates, SEC filings, and structure-risk flags.
- **News/event layer**: Marketaux headlines plus FMP filings/company-background factors.
- **Forecast layer**: 15m-1h path probabilities, primary path, confirmation condition, invalidation condition, and confidence.
- **De-duplication layer**: semantic state keys, material price displacement checks, cooldowns, and forecast-history evaluation.

The result is a quieter alert stream that focuses on new information instead of repeating old anxiety.

## Free-Key First

This project is designed around **completely free API keys / free-tier keys**. You do not need paid market-data subscriptions to use it.

Supported free-key providers:

- Finnhub free API key for quotes.
- Twelve Data free API key for quotes and candles.
- Financial Modeling Prep free API key for company context: profile, peers, float/liquidity, report dates, SEC filings, and selected event data where your free plan allows it.
- Alpha Vantage free API key for daily candle fallback.
- Marketaux free API key for simple news context.
- Yahoo Finance fallback without an API key.

Free APIs can be delayed, rate-limited, plan-limited, or temporarily unavailable. The script treats each provider as optional and falls back automatically where possible.

## Designed For Hermes And OpenClaw

The script prints alert text only when the forecast state, key levels, or material price movement changes enough to matter. This makes it suitable for:

- Hermes cron jobs
- OpenClaw scheduled tasks
- QQ/Telegram/Discord bot bridges
- Any cron wrapper that sends non-empty stdout

If nothing actionable happened, the command exits silently.

## Features

- Multi-source quote fallback: Finnhub, Twelve Data, Yahoo Finance, with FMP quote used only when your free plan allows it.
- Candle fallback: Twelve Data, Yahoo Finance, Alpha Vantage, with FMP EOD used only when your free plan allows it.
- Daily context: 3/5/20 day returns, support/resistance, ATR percentile, gap, volume anomaly, relative strength vs `SPY`, `QQQ`, and `SMH`.
- FMP company context layer: sector/industry, peer basket, float structure, report dates, SEC filings, M&A/event hints, and structure-risk flags.
- Prediction layer: 15m-1h path probabilities, confirmation and invalidation levels.
- Alert de-duplication: suppresses repeated alerts unless forecast state, key levels, or price displacement materially changes.
- Forecast history and later evaluation data stored in a local JSON state file.
- Optional Marketaux news factor plus FMP filings/company-background factors.
- Cron-native silent mode: stdout stays empty when there is no alert, which keeps bot integrations clean.
- Provider-aware fallback: missing keys, rate limits, plan-limited endpoints, and stale quotes are handled gracefully.
- Human-readable alert cards: each alert includes thesis, action bias, context, probabilities, validation levels, and key evidence.

## Requirements

- Python 3.10+
- `curl` available on PATH
- Free API keys for the providers you want to enable

The script uses only Python standard-library modules.

## Setup

```bash
cp .env.example .env
python quant_alert_predictor.py --print-config
```

Fill the free keys you have in `.env`. You can run with only Yahoo fallback, but quote freshness and daily/news context improve when you add free-tier keys.

## Usage

```bash
python quant_alert_predictor.py --symbol SNDK
python quant_alert_predictor.py --symbol AAPL --state-file ./state/aapl_state.json
python quant_alert_predictor.py --symbol NVDA --env-file /path/to/.env
```

The command exits silently when no alert should be sent. If it prints text, forward that text through Hermes, OpenClaw, your bot bridge, or your notification system.

## Environment

Supported provider variables:

```bash
FINNHUB_API_KEY=
TWELVE_DATA_API_KEY=
FMP_API_KEY=
ALPHA_VANTAGE_API_KEY=
MARKETAUX_API_KEY=
```

Runtime defaults:

```bash
QAP_SYMBOL=SNDK
QAP_ENV_PATH=./.env
QAP_STATE_PATH=./state/sndk_state.json
```

CLI flags override environment defaults.

## Hermes / OpenClaw Cron Example

Run every minute and forward only non-empty output:

```bash
* * * * * cd /opt/pulse-harbor && python3 quant_alert_predictor.py --symbol SNDK >> /tmp/qap_sndk.out 2>> /tmp/qap_sndk.err
```

In Hermes or OpenClaw, use the same command as a scheduled task and send stdout only when stdout is non-empty.

## Roadmap

Stars are welcome if this project helps you. PulseHarbor will keep adding integrations for more useful, high-quality, completely free APIs.

## Notes

This is an alert and forecasting aid, not financial advice. Free data APIs can be delayed, incomplete, rate-limited, or plan-limited. The script treats unavailable optional providers as fallbacks and keeps running where possible.
