---
name: trade-fourmeme
description: "Buy or sell tokens on Four.Meme bonding curve using the official fourmeme CLI. Use when user wants to trade on Four.Meme, get token rankings, or check bonding curve tokens."
metadata: {"nanobot":{"emoji":"🟢"}}
---

# Four.Meme Trading (Official CLI)

Trade tokens on Four.Meme bonding curves using the `fourmeme` CLI tool.

## When to use

Trigger when the user:
- Says "buy [token] on four.meme" or "ape [amount] BNB into [token]"
- Says "sell [token] on four.meme" or "dump my [token]"
- Wants to see trending/hot tokens on Four.Meme
- Asks about a token on the bonding curve
- Wants to trade a token that hasn't graduated yet

## Available Commands

Run these via the exec tool:

```bash
# Platform config and BNB price
fourmeme config

# Token rankings
fourmeme token-rankings --type hot          # trending tokens
fourmeme token-rankings --type volume24h    # 24h volume leaders
fourmeme token-rankings --type newest       # newest launches
fourmeme token-rankings --type graduated    # recently graduated

# Token details
fourmeme token-info --address 0xABC         # full token info
fourmeme token-list                         # list/filter tokens

# Price quotes (ALWAYS do this before executing trades)
fourmeme quote-buy --token 0xABC --amount 0.01   # estimate buy (amount in BNB)
fourmeme quote-sell --token 0xABC --amount 1000  # estimate sell (amount in tokens)

# Execute trades
fourmeme buy --token 0xABC --amount 0.01    # buy with BNB
fourmeme sell --token 0xABC --amount 1000   # sell tokens

# Send tokens or BNB
fourmeme send --to 0xABC --amount 0.01 --token BNB

# EIP-8004 Agent Identity
fourmeme 8004-register                          # register agent NFT
fourmeme 8004-balance --address 0xABC           # check agent NFT balance
```

## Trading Rules — FOLLOW STRICTLY

1. **ALWAYS quote-buy or quote-sell BEFORE executing** — show the user what they'll get and ask for confirmation
2. **Check BNB balance before trading** — use `fourmeme config` or on-chain check
3. **USD amount conversion** — if user says "$5 of token X", get BNB price from `fourmeme config` first, then convert: `bnb_amount = usd_amount / bnb_price`
4. **Maximum trade size: 0.1 BNB** unless user has a higher `max_trade_bnb` set in bot_users
5. **Minimum trade size: 0.001 BNB** — below this, gas makes the trade pointless
6. **Slippage**: the CLI handles slippage internally

## Trade Flow

1. User requests a buy/sell
2. Run `fourmeme quote-buy` or `fourmeme quote-sell` to get estimated output
3. Show user: "You'll get ~X tokens for Y BNB. Proceed?"
4. On confirmation, run `fourmeme buy` or `fourmeme sell`
5. Return the tx hash and summary

## Notes

- The CLI reads PRIVATE_KEY from /app/.env for signing
- For per-user wallets: the bot decrypts the user's key and can set PRIVATE_KEY before calling the CLI
- The CLI handles gas estimation and transaction submission automatically
- Graduated tokens (liquidityAdded=true) should be routed to PancakeSwap instead
