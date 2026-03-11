---
name: withdraw_token
always: true
---

# Token Withdrawal Skill

Withdraw (transfer) ERC-20 tokens from the user's FlagentBot wallet to any BSC address.

## When to use

Users may ask in many ways:
- "/withdraw_token 0xTokenCA 0xDestination 1000"
- "/withdraw_token 0xTokenCA 0xDestination all"
- "send my ENEMY tokens to 0xMyWallet"
- "withdraw my tokens to 0x..."
- "transfer 1000 TOKEN to my metamask 0x..."
- "send all my 0xTokenCA to 0x..."

All of these should trigger the withdraw_token script.

## Usage

```
python3 workspace/skills/withdraw_token/scripts/withdraw_token.py <telegram_user_id> <token_address> <destination_address> <amount>
```

- `amount` can be a number (raw token units) or `all` to send the full balance
- Returns JSON with tx_hash, token, amount, destination, symbol
- No fee on withdrawals — fees are only on trades

## Important

- Always confirm with the user before executing (YES/NO flow handled in loop.py)
- Validate both token and destination addresses (0x + 40 hex chars)
- If amount is "all", reads on-chain balanceOf for actual holdings
