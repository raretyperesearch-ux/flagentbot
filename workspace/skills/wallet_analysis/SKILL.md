---
name: wallet-analysis
description: "Analyze any BSC wallet address — balance, transactions, token transfers, trading patterns, and security flags. Use when user asks about a wallet, sends a non-token address, or asks 'check this wallet'."
metadata: {"nanobot":{"emoji":"👛"}}
---

# Wallet Analysis

Analyze any BSC wallet address using BSCScan API and GoPlus address security.

## When to use

Trigger when the user:
- Asks "check this wallet" / "who is this address?" / "wallet analysis"
- Sends a 0x address that is NOT a token contract (use context to distinguish)
- Asks about their own wallet activity or another wallet's trading history
- Wants to know if an address is a bot, whale, or scammer

## API keys

- `BSCSCAN_API_KEY` env var (required for BSCScan endpoints). Free tier: 5 calls/sec.
- GoPlus address security: no key required.

## Analysis flow

Run all four BSCScan calls + GoPlus in parallel, then combine.

### 1. BNB balance

```python
import httpx, os

BSCSCAN_KEY = os.environ.get("BSCSCAN_API_KEY", "")
BSCSCAN = "https://api.bscscan.com/api"

async def get_bnb_balance(address: str) -> str:
    """Get BNB balance in ether units."""
    params = {
        "module": "account", "action": "balance",
        "address": address, "tag": "latest", "apikey": BSCSCAN_KEY,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(BSCSCAN, params=params)
        data = resp.json()
    wei = int(data.get("result", "0"))
    return f"{wei / 1e18:.4f}"
```

### 2. Last 10 transactions

```python
async def get_recent_txns(address: str, count: int = 10) -> list[dict]:
    """Get most recent normal transactions."""
    params = {
        "module": "account", "action": "txlist",
        "address": address, "startblock": 0, "endblock": 99999999,
        "page": 1, "offset": count, "sort": "desc", "apikey": BSCSCAN_KEY,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(BSCSCAN, params=params)
        data = resp.json()
    return data.get("result", []) if isinstance(data.get("result"), list) else []
```

### 3. Last 20 token transfers

```python
async def get_token_transfers(address: str, count: int = 20) -> list[dict]:
    """Get most recent BEP-20 token transfers."""
    params = {
        "module": "account", "action": "tokentx",
        "address": address, "startblock": 0, "endblock": 99999999,
        "page": 1, "offset": count, "sort": "desc", "apikey": BSCSCAN_KEY,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(BSCSCAN, params=params)
        data = resp.json()
    return data.get("result", []) if isinstance(data.get("result"), list) else []
```

### 4. GoPlus address security

```python
async def goplus_address_security(address: str) -> dict:
    """Check if address is flagged as malicious."""
    url = f"https://api.gopluslabs.io/api/v1/address_security/{address.lower()}?chain_id=56"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        data = resp.json()
    return data.get("result", {})
```

Key GoPlus fields:
- `cybercrime` — "1" if flagged as cybercrime
- `money_laundering` — "1" if flagged
- `phishing_activities` — "1" if phishing
- `blacklist_doubt` — "1" if on blacklists
- `contract_address` — "1" if this is a contract, not an EOA

## Platform interaction counting

Count interactions with known BSC platforms from the transaction list:

```python
FOUR_MEME_ROUTER = "0x5c952063c7fc8610ffdb798152d69f0b9550762b"
FLAP_PORTAL = "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0"

def count_platform_activity(txns: list[dict], token_transfers: list[dict]) -> dict:
    """Count interactions with Four.Meme and Flap.sh from transaction history."""
    four_meme_count = 0
    flap_count = 0
    unique_tokens = set()

    for tx in txns:
        to_addr = (tx.get("to") or "").lower()
        if to_addr == FOUR_MEME_ROUTER:
            four_meme_count += 1
        # Flap.sh detection: check known addresses or input data patterns
        if to_addr == FLAP_PORTAL:
            flap_count += 1

    for tt in token_transfers:
        unique_tokens.add(tt.get("contractAddress", "").lower())

    return {
        "four_meme_txns": four_meme_count,
        "flap_sh_txns": flap_count,
        "unique_tokens_touched": len(unique_tokens),
    }
```

## Report format

```
Wallet: {address}
BNB Balance: {balance} BNB

Security:
  Cybercrime: {flag}
  Phishing: {flag}
  Blacklisted: {flag}
  Type: {EOA / Contract}

Activity (last 10 txns):
  Total txns scanned: {count}
  Four.Meme interactions: {count}
  Flap.sh interactions: {count}
  Unique tokens touched: {count}

Recent Transactions:
  1. {hash[:16]}... | {method} | {value} BNB | {timeAgo}
  2. ...

Recent Token Transfers:
  1. {tokenSymbol} | {from → to} | {value} | {timeAgo}
  2. ...
```

## Notes

- BSCScan free tier is 5 requests/second. Space calls or use async gather.
- If `BSCSCAN_API_KEY` is missing, all BSCScan calls will return rate-limit errors.
- Four.Meme router address `0x5c952063c7fc8610ffdb798152d69f0b9550762b` is the primary buy/sell entry point.
- To distinguish a token address from a wallet address: token contracts have code, wallets (EOAs) do not. GoPlus `contract_address` field helps.
