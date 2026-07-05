# PulseHarbor

English | [中文](README.zh-CN.md)

PulseHarbor is a free-key market monitor and short-window predictor designed for bot and automation runtimes such as **Hermes** and **OpenClaw**.

It is built for cron-style alert pipelines: run it frequently, forward stdout only when it prints something, and stay silent when there is no meaningful change.

## Free-Key First

This project is designed around **completely free API keys / free-tier keys**. You do not need paid market-data subscriptions to use it.

Supported free-key providers:

- Finnhub free API key for quotes.
- Twelve Data free API key for quotes and candles.
- Financial Modeling Prep free API key for quote/EOD/news fallback where your free plan allows it.
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

- Multi-source quote fallback: Finnhub, Financial Modeling Prep, Twelve Data, Yahoo Finance.
- Candle fallback: Twelve Data, FMP EOD, Alpha Vantage, Yahoo Finance.
- Daily context: 3/5/20 day returns, support/resistance, ATR percentile, gap, volume anomaly, relative strength vs `SPY`, `QQQ`, and `SMH`.
- Prediction layer: 15m-1h path probabilities, confirmation and invalidation levels.
- Alert de-duplication: suppresses repeated alerts unless forecast state, key levels, or price displacement materially changes.
- Forecast history and later evaluation data stored in a local JSON state file.
- Optional Marketaux and FMP news factor.

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

## Notes

This is an alert and forecasting aid, not financial advice. Free data APIs can be delayed, incomplete, rate-limited, or plan-limited. The script treats unavailable optional providers as fallbacks and keeps running where possible.
