---
name: trade-fourmeme
description: "Buy or sell tokens on the Four.Meme bonding curve (BSC). Use when user says 'buy on four.meme', 'sell on four.meme', 'ape into [token]', or wants to trade a token that is still on the bonding curve."
metadata: {"nanobot":{"emoji":"🟢"}}
---

# Four.Meme Trading

Execute buy or sell transactions on the Four.Meme bonding curve via TokenManager2 on BSC.

## When to use

Trigger when the user:
- Says "buy [token] on four.meme" or "ape [amount] BNB into [token]"
- Says "sell [token] on four.meme" or "dump my [token]"
- Wants to trade a token that is still on the bonding curve (not yet graduated)

## Requirements

- `ENCRYPTION_KEY` — for decrypting user's private key
- `SUPABASE_SERVICE_KEY` — for reading bot_users and logging to bot_positions
- BSC RPC: `https://bsc-dataseed.binance.org`

## Contracts

- TokenManager2: `0x5c952063c7fc8610FFDB798152D69F0B9550762b`
- TokenManagerHelper3: `0xF251F83e40a78868FcfA3FA4599Dad6494E46034`
- Treasury: `0x6c8C4C62183B61E9dd0095e821B0F857b555b32d`

## Script usage

```bash
# Buy
python scripts/trade_fourmeme.py <telegram_user_id> buy <token_address> <bnb_amount>

# Sell
python scripts/trade_fourmeme.py <telegram_user_id> sell <token_address> <amount_or_percent>
```

## Flow

1. Read encrypted wallet from `bot_users` via Supabase
2. Decrypt private key with AES-256-GCM
3. For buys: deduct 0.5% BNB fee → send fee to treasury → call `buyTokenAMAP(token, funds, 0)`
4. For sells: call `balanceOf` → `approve(TM2, amount)` → `sellToken(token, amount)`
5. Log trade to `bot_positions` table
6. Return tx hash and summary
