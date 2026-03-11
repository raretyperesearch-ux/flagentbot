---
name: Four.Meme Trading
description: Buy and sell tokens on Four.Meme bonding curves using the official CLI
always: true
---

# Four.Meme Trading (Official CLI)

Trade tokens on Four.Meme using the official `fourmeme` CLI.
The helper script handles per-user wallet decryption automatically.

## Commands

### Get token info (on-chain)
```bash
fourmeme token-info <tokenAddress>
```
Returns: version, tokenManager, price, offers, graduation status. No auth needed.

### Get token rankings
```bash
fourmeme token-rankings Hot
fourmeme token-rankings TradingDesc --barType=HOUR24
fourmeme token-rankings Time
```

### Quote buy (estimate only, no transaction)
```bash
python3 /root/.nanobot/workspace/skills/shared/scripts/fourmeme_exec.py {telegram_user_id} quote-buy {token_address} 0 {funds_wei}
```
Use amountWei=0 and fundsWei={BNB in wei} to estimate "spend X BNB".
1 BNB = 1000000000000000000 wei. 0.01 BNB = 10000000000000000 wei.

### Execute buy (spend BNB to buy tokens)
```bash
python3 /root/.nanobot/workspace/skills/shared/scripts/fourmeme_exec.py {telegram_user_id} buy {token_address} funds {funds_wei} {min_amount_wei}
```
- funds_wei: BNB to spend in wei
- min_amount_wei: minimum tokens to receive (use 0 for no slippage protection, or calculate 95% of quote for 5% slippage)

### Quote sell (estimate only)
```bash
python3 /root/.nanobot/workspace/skills/shared/scripts/fourmeme_exec.py {telegram_user_id} quote-sell {token_address} {amount_wei}
```

### Execute sell
```bash
python3 /root/.nanobot/workspace/skills/shared/scripts/fourmeme_exec.py {telegram_user_id} sell {token_address} {amount_wei} {min_funds_wei}
```
- amount_wei: tokens to sell in wei (token decimals, usually 18)
- min_funds_wei: optional, minimum BNB to receive

## Rules
1. ALWAYS quote-buy or quote-sell FIRST — show user the estimate before executing
2. Convert USD to BNB if user specifies dollars (fetch BNB price from DexScreener)
3. Max trade 0.1 BNB unless user explicitly says more
4. The telegram_user_id comes from the current chat session
5. All fourmeme commands output JSON — parse and present results naturally
6. Only V2 tokens are supported (version=2 from token-info)

## Wei conversion
- 1 BNB = 1e18 wei = 1000000000000000000
- 0.01 BNB = 1e16 = 10000000000000000
- 0.001 BNB = 1e15 = 1000000000000000
