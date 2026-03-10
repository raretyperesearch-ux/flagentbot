---
name: trade-flapsh
description: "Buy or sell tokens on the Flap.sh Portal (BSC). Use when user says 'buy on flap', 'sell on flap.sh', or wants to trade a Flap.sh token."
metadata: {"nanobot":{"emoji":"🦋"}}
---

# Flap.sh Trading

Execute buy or sell transactions on the Flap.sh Portal contract on BSC.

## When to use

Trigger when the user:
- Says "buy [token] on flap.sh" or "ape into [token] on flap"
- Says "sell [token] on flap.sh"
- Wants to trade a token launched via Flap.sh

## Requirements

- `ENCRYPTION_KEY` — for decrypting user's private key
- `SUPABASE_SERVICE_KEY` — for reading bot_users and logging to bot_positions
- BSC RPC: `https://bsc-dataseed.binance.org`

## Contracts

- Flap.sh Portal: `0xe2cE6ab80874Fa9Fa2aAE65D277Dd6B8e65C9De0`
- Treasury: `0x6c8C4C62183B61E9dd0095e821B0F857b555b32d`

## Script usage

```bash
# Buy
python scripts/trade_flapsh.py <telegram_user_id> buy <token_address> <bnb_amount>

# Sell
python scripts/trade_flapsh.py <telegram_user_id> sell <token_address> <amount_or_percent>
```

## Flow

1. Read encrypted wallet from `bot_users` via Supabase
2. Decrypt private key with AES-256-GCM
3. For buys: deduct 0.5% BNB fee → send fee to treasury → call `buy(token, recipient, 0)` with value
4. For sells: call `balanceOf` → `approve(Portal, amount)` → `swapExactInput((token, 0x0, amount, 0, 0x))`
5. Log trade to `bot_positions` table
6. Return tx hash and summary
