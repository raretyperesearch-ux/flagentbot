---
name: trade-pancakeswap
description: "Buy or sell graduated tokens on PancakeSwap V3 (BSC). Use when user wants to trade a token that has already graduated from the bonding curve and has liquidity on PancakeSwap."
metadata: {"nanobot":{"emoji":"🥞"}}
---

# PancakeSwap V3 Trading

Execute buy or sell transactions on PancakeSwap V3 SmartRouter for graduated BSC tokens.

## When to use

Trigger when the user:
- Wants to trade a token that has graduated from Four.Meme or Flap.sh
- Says "buy on pancakeswap" or "swap BNB for [token]"
- Says "sell on pancakeswap" or "swap [token] for BNB"
- Token analysis shows `liquidity_added: true`

## Requirements

- `ENCRYPTION_KEY` — for decrypting user's private key
- `SUPABASE_SERVICE_KEY` — for reading bot_users and logging to bot_positions
- BSC RPC: `https://bsc-dataseed.binance.org`

## Contracts

- PancakeSwap V3 SmartRouter: `0x13f4EA83D0bd40E75C8222255bc855a974568Dd4`
- WBNB: `0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c`
- Treasury: `0x6c8C4C62183B61E9dd0095e821B0F857b555b32d`

## Script usage

```bash
# Buy (swap BNB → token)
python scripts/trade_pancakeswap.py <telegram_user_id> buy <token_address> <bnb_amount>

# Sell (swap token → BNB)
python scripts/trade_pancakeswap.py <telegram_user_id> sell <token_address> <amount_or_percent>
```

## Flow

1. Read encrypted wallet from `bot_users` via Supabase
2. Decrypt private key with AES-256-GCM
3. For buys: deduct 0.5% fee → send fee to treasury → `exactInputSingle` (WBNB → token) with value
4. For sells: `balanceOf` → `approve(router, amount)` → `exactInputSingle` (token → WBNB)
5. Log trade to `bot_positions` table
6. Return tx hash and summary
