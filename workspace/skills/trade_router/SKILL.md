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

## Detection Logic

1. **Check Four.Meme first**: run `fourmeme token-info --address <token>`
   - If found + not graduated → route to **Four.Meme CLI** (`fourmeme buy`/`fourmeme sell`)
   - If found + graduated → route to **PancakeSwap** skill
2. **If not on Four.Meme**: try PancakeSwap V3 quote
   - If quote succeeds → route to **PancakeSwap** skill
3. **If nothing works** → tell user "Token not found on any supported platform"

## Safety Guards (apply to ALL routes)

- **Max trade: 0.1 BNB** (unless user has higher `max_trade_bnb` in bot_users)
- **Min trade: 0.001 BNB** (below this, gas makes it pointless)
- **USD conversion**: if user says "$5 of token", get BNB price first and convert
- **Balance check**: ensure user has enough BNB before executing
- **Quote first**: always show user what they'll get before executing

## Routing Commands

### Four.Meme (bonding curve tokens)
```bash
fourmeme token-info --address 0xABC    # detect platform
fourmeme quote-buy --token 0xABC --amount 0.01  # get quote
fourmeme buy --token 0xABC --amount 0.01        # execute
fourmeme quote-sell --token 0xABC --amount 1000
fourmeme sell --token 0xABC --amount 1000
```

### PancakeSwap (graduated tokens)
```bash
python scripts/trade_router.py <telegram_user_id> buy <token_address> <bnb_amount>
python scripts/trade_router.py <telegram_user_id> sell <token_address> <amount_or_percent>
```

## Contracts

- Four.Meme Helper3: `0xF251F83e40a78868FcfA3FA4599Dad6494E46034`
- Four.Meme TokenManager2: `0x5c952063c7fc8610FFDB798152D69F0B9550762b`
- Flap.sh Portal: `0xe2cE6ab80874Fa9Fa2aAE65D277Dd6B8e65C9De0`
- PancakeSwap V3 SmartRouter: `0x13f4EA83D0bd40E75C8222255bc855a974568Dd4`
