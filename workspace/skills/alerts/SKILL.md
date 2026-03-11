---
name: alerts
always: true
---

# Alerts Skill

Set and manage price alerts, wallet watches, volume spikes, and new token notifications.

## Alert Types

- **Price target**: "alert me when TOKEN hits 0.001 BNB" — triggers when token crosses a price threshold
- **Wallet watch**: "alert me when 0xWallet buys something" — triggers on new transactions from a wallet
- **Volume spike**: "alert me when Four.Meme volume spikes" — triggers on 100%+ volume increase
- **New token**: "alert me when AI tokens launch on Four.Meme" — triggers when a new token matching a keyword is created

## Usage

When a user wants to set an alert, run:
```
python3 workspace/skills/alerts/scripts/manage_alerts.py set <telegram_user_id> <alert_type> '<config_json>'
```

To list alerts:
```
python3 workspace/skills/alerts/scripts/manage_alerts.py list <telegram_user_id>
```

To delete an alert:
```
python3 workspace/skills/alerts/scripts/manage_alerts.py delete <telegram_user_id> <alert_id>
```

## Alert types and config format

- `price_target`: `{"token": "0x...", "target_bnb": 0.001, "direction": "above"}`
- `wallet_watch`: `{"wallet": "0x...", "last_tx_hash": ""}`
- `volume_spike`: `{"platform": "four_meme", "baseline_volume": 0}`
- `new_token`: `{"keyword": "AI"}`

## Checking alerts

Alerts are checked every 5 minutes by a cron job that runs:
```
python3 workspace/skills/alerts/scripts/check_alerts.py
```

When an alert triggers, the script outputs JSON with the alert details and chat_id. The agent should forward the alert message to the user.
