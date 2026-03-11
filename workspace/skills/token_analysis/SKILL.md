---
name: token-analysis
description: "Analyze any BSC token by contract address. Use when user sends a contract address (0x...) or asks about a token's security, bonding curve, or honeypot status."
metadata: {"nanobot":{"emoji":"🔍"}}
---

# Token Analysis

Analyze any BSC (BNB Smart Chain) token by contract address. Combines GoPlus security data with Four.Meme bonding curve state.

## When to use

Trigger immediately when the user:
- Sends a contract address (0x...)
- Asks "is this token safe?" / "check this token" / "ape check"
- Asks about honeypot status, taxes, holders, or bonding curve
- Mentions a token and wants security analysis

## Analysis flow

Run both data sources in parallel, then combine into a single report.

### 1. GoPlus Security API (no key required)

```python
import httpx, json

async def goplus_token_security(address: str) -> dict:
    """Fetch GoPlus security report for a BSC token."""
    url = f"https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses={address.lower()}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        data = resp.json()
    result = data.get("result", {})
    # GoPlus keys results by lowercase address
    return result.get(address.lower(), {})
```

Key fields to extract from GoPlus response:
- `token_name`, `token_symbol` — name and ticker
- `holder_count` — number of holders
- `is_honeypot` — "1" means honeypot (cannot sell)
- `buy_tax`, `sell_tax` — tax percentages (as strings like "0.05" = 5%)
- `is_open_source` — "1" means verified source code
- `is_proxy` — "1" means upgradeable proxy (risky)
- `is_mintable` — "1" means owner can mint new tokens
- `owner_address` — current owner
- `lp_holder_count` — number of LP holders
- `lp_total_supply` — total LP tokens
- `holders` — array of top holders with `address`, `percent`, `is_locked`, `is_contract`

### 2. Four.Meme Official CLI (bonding curve status)

Use the `fourmeme` CLI to get token details from Four.Meme:

```bash
# Get full token info (bonding curve status, price, supply, etc.)
fourmeme token-info --address 0xABC

# Get trending tokens on Four.Meme
fourmeme token-rankings --type hot
fourmeme token-rankings --type volume24h
fourmeme token-rankings --type newest
fourmeme token-rankings --type graduated
```

The `token-info` response includes bonding curve data: current price, funds raised, graduation status, and trading fees. Use this instead of raw eth_call for reliability.

### 3. Combining into a report

After fetching both sources, present a structured report:

```
Token: {name} ({symbol})
Contract: {address}
Holders: {holder_count}

Security:
  Honeypot: {Yes/No}
  Buy Tax: {buy_tax}% | Sell Tax: {sell_tax}%
  Open Source: {Yes/No}
  Mintable: {Yes/No}
  Proxy: {Yes/No}
  Owner: {owner_address}

Four.Meme Status:
  On Bonding Curve: {Yes/No}
  {bonding curve details if available}

Top Holders:
  1. {address} — {percent}% {locked?}
  2. ...
```

## Risk flags

Highlight these as warnings:
- `is_honeypot == "1"` — CRITICAL: Cannot sell
- `sell_tax > 0.10` — HIGH: >10% sell tax
- `is_mintable == "1"` — WARN: Owner can inflate supply
- `is_proxy == "1"` — WARN: Contract can be changed
- Top holder owns >20% — WARN: Whale concentration
- `is_open_source == "0"` — WARN: Unverified code

## Notes

- GoPlus API is free and rate-limited. If it returns empty, the token may be too new.
- Four.Meme tokens that have "graduated" are now on PancakeSwap v3 — bonding curve data may show zeroes.
- Always show the raw contract address so the user can verify on BSCScan.
