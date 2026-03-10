---
name: trade-router
description: "Smart trade router — auto-detects the correct platform for any BSC token. Use when user says 'buy [token]' or 'sell [token]' without specifying a platform."
always: true
metadata: {"nanobot":{"emoji":"🔀"}}
---

# Smart Trade Router

Automatically detects the correct trading platform for any BSC token and executes the trade.

## When to use

Trigger when the user:
- Says "buy 0.01 BNB of 0xABC..." without specifying a platform
- Says "sell my 0xABC..." without specifying a platform
- Says "ape into [token]" or "dump [token]"
- Wants to trade any token and doesn't know which platform it's on

## Detection logic

1. Call Four.Meme Helper3 `getTokenInfo(token)`
2. If returns data + `liquidityAdded=false` → route to Four.Meme bonding curve
3. If returns data + `liquidityAdded=true` → route to PancakeSwap (graduated)
4. If no Four.Meme data → try PancakeSwap quote
5. If nothing works → tell user "Token not found on any supported platform"

## Script usage

```bash
# Buy
python scripts/trade_router.py <telegram_user_id> buy <token_address> <bnb_amount>

# Sell
python scripts/trade_router.py <telegram_user_id> sell <token_address> <amount_or_percent>
```

## Contracts

- Four.Meme Helper3: `0xF251F83e40a78868FcfA3FA4599Dad6494E46034`
- Four.Meme TokenManager2: `0x5c952063c7fc8610FFDB798152D69F0B9550762b`
- Flap.sh Portal: `0xe2cE6ab80874Fa9Fa2aAE65D277Dd6B8e65C9De0`
- PancakeSwap V3 SmartRouter: `0x13f4EA83D0bd40E75C8222255bc855a974568Dd4`
