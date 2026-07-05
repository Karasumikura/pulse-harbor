# Hermes Cron Example

The script is bot-agnostic: it prints an alert only when something actionable changed. In Hermes, create a no-agent cron job that runs the script and sends stdout when non-empty.

Example command:

```bash
cd /opt/pulse-harbor && python3 quant_alert_predictor.py --symbol SNDK --env-file /opt/pulse-harbor/.env --state-file /opt/pulse-harbor/state/sndk_state.json
```

Suggested cadence:

```cron
* * * * *
```

The script has its own market-session cadence gate, so an every-minute cron does not mean every-minute API usage. During quiet periods it updates state and exits silently.
