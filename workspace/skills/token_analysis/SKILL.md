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

### 2. Four.Meme TokenManagerHelper3 (bonding curve status)

Contract: `0xF251F83e40a78868FcfA3FA4599Dad6494E46034` on BSC mainnet.

```python
import httpx

# TokenManagerHelper3 getTokenInfo(address) → returns bonding curve data
# Function selector: keccak256("getTokenInfo(address)")[:4]
TOKEN_MANAGER_HELPER = "0xF251F83e40a78868FcfA3FA4599Dad6494E46034"
BSC_RPC = "https://bsc-dataseed1.binance.org"

async def four_meme_token_info(token_address: str) -> dict:
    """Call Four.Meme TokenManagerHelper3.getTokenInfo() via eth_call."""
    # Encode: getTokenInfo(address)
    # selector = 0x1a7a98e2 (first 4 bytes of keccak256("getTokenInfo(address)"))
    padded_addr = token_address.lower().replace("0x", "").zfill(64)
    calldata = "0x1a7a98e2" + padded_addr

    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": TOKEN_MANAGER_HELPER, "data": calldata}, "latest"]
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(BSC_RPC, json=payload)
        result = resp.json().get("result", "0x")

    if result == "0x" or len(result) < 66:
        return {"on_four_meme": False}

    # Decode response — the return is a struct with multiple uint256 fields
    # Key fields (each 32 bytes / 64 hex chars after 0x prefix):
    # The exact layout depends on the contract version, but typically includes:
    # - raisedAmount (how much BNB raised in bonding curve)
    # - totalSupply (token supply in curve)
    # - graduated (bool — whether token has left bonding curve)
    hex_data = result[2:]  # strip 0x
    chunks = [hex_data[i:i+64] for i in range(0, len(hex_data), 64)]

    return {
        "on_four_meme": True,
        "raw_chunks": chunks,
        "chunk_count": len(chunks),
    }
```

The return struct layout (from the official ABI) is:
```
[0] version: uint256
[1] tokenManager: address
[2] quote: address          — 0x0 if still on bonding curve
[3] lastPrice: uint256      — current price in wei
[4] tradingFeeRate: uint256
[5] minTradingFee: uint256
[6] launchTime: uint256
[7] offers: uint256         — remaining token supply in curve
[8] maxOffers: uint256      — total token supply allocated to curve
[9] funds: uint256          — BNB raised so far
[10] maxFunds: uint256      — BNB target for graduation
[11] liquidityAdded: bool   — true = graduated to PancakeSwap
```

Bonding progress = `100 - (offers * 100 / maxOffers)` percent.
Token is still on curve if `liquidityAdded == false && quote == 0x0000...`.

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
