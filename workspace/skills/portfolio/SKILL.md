---
name: portfolio
description: "Show the user's BSC wallet portfolio — BNB balance, token positions, PnL, and trade history. Use when user says 'my portfolio', 'my positions', 'my balance', 'show my wallet', or 'what do I hold'."
metadata: {"nanobot":{"emoji":"💼"}}
---

# Portfolio

Show the user's BSC wallet balance, open positions, PnL, and recent trade history.

## When to use

Trigger when the user:
- Says "my portfolio", "my balance", "my positions", "what do I hold"
- Asks "how much BNB do I have" or "show my wallet"
- Asks about profit/loss or trade history

## Requirements

- `ENCRYPTION_KEY` — for decrypting user's wallet address (not private key — only address needed)
- `SUPABASE_SERVICE_KEY` — for reading bot_users and bot_positions
- `BSCSCAN_API_KEY` — for on-chain balance queries

## Script usage

```bash
python scripts/portfolio.py <telegram_user_id>
```

## Data sources

1. `bot_users` — user's wallet address
2. `bot_positions` — trade history (token, side, amount, price, tx_hash, timestamp)
3. BSCScan API — live BNB balance and token balances
4. GoPlus — current token names/symbols

## Report format

```
Portfolio for 0x1234...abcd

BNB Balance: 1.2345 BNB

Open Positions:
  1. TOKEN1 — 1,000,000 tokens | Bought: 0.05 BNB | Current: TBD
  2. TOKEN2 — 500,000 tokens | Bought: 0.10 BNB | Current: TBD

Recent Trades:
  1. BUY TOKEN1 | 0.05 BNB | 2h ago | tx: 0xabc...
  2. SELL TOKEN3 | 0.03 BNB | 1d ago | tx: 0xdef...

Total Invested: 0.15 BNB
```
