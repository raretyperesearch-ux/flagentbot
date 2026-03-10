"""Wallet analysis script — BSCScan + GoPlus security for any BSC address."""

import asyncio
import json
import os
import sys
from datetime import datetime

import httpx

BSCSCAN_KEY = os.environ.get("BSCSCAN_API_KEY", "")
BSCSCAN = "https://api.bscscan.com/api"
FOUR_MEME_ROUTER = "0x5c952063c7fc8610ffdb798152d69f0b9550762b"
FLAP_PORTAL = "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0"


async def _bscscan(params: dict) -> dict:
    params["apikey"] = BSCSCAN_KEY
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(BSCSCAN, params=params)
        resp.raise_for_status()
        return resp.json()


async def get_bnb_balance(address: str) -> str:
    data = await _bscscan({"module": "account", "action": "balance", "address": address, "tag": "latest"})
    wei = int(data.get("result", "0"))
    return f"{wei / 1e18:.4f}"


async def get_recent_txns(address: str, count: int = 10) -> list[dict]:
    data = await _bscscan({
        "module": "account", "action": "txlist", "address": address,
        "startblock": 0, "endblock": 99999999,
        "page": 1, "offset": count, "sort": "desc",
    })
    result = data.get("result", [])
    return result if isinstance(result, list) else []


async def get_token_transfers(address: str, count: int = 20) -> list[dict]:
    data = await _bscscan({
        "module": "account", "action": "tokentx", "address": address,
        "startblock": 0, "endblock": 99999999,
        "page": 1, "offset": count, "sort": "desc",
    })
    result = data.get("result", [])
    return result if isinstance(result, list) else []


async def goplus_address_security(address: str) -> dict:
    url = f"https://api.gopluslabs.io/api/v1/address_security/{address.lower()}?chain_id=56"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        return resp.json().get("result", {})


def time_ago(timestamp_str: str) -> str:
    try:
        ts = int(timestamp_str)
        delta = datetime.now().timestamp() - ts
        if delta < 3600:
            return f"{int(delta / 60)}m ago"
        if delta < 86400:
            return f"{int(delta / 3600)}h ago"
        return f"{int(delta / 86400)}d ago"
    except (ValueError, TypeError):
        return "?"


def count_platform_activity(txns: list[dict], transfers: list[dict]) -> dict:
    four_meme = 0
    flap_sh = 0
    for tx in txns:
        to = (tx.get("to") or "").lower()
        if to == FOUR_MEME_ROUTER:
            four_meme += 1
        elif to == FLAP_PORTAL:
            flap_sh += 1
    unique_tokens = {t.get("contractAddress", "").lower() for t in transfers if t.get("contractAddress")}
    return {"four_meme_txns": four_meme, "flap_sh_txns": flap_sh, "unique_tokens": len(unique_tokens)}


def format_report(
    address: str, balance: str, txns: list[dict],
    transfers: list[dict], security: dict, activity: dict,
) -> str:
    lines = [
        f"Wallet: {address}",
        f"BNB Balance: {balance} BNB",
        "",
        "Security:",
    ]

    flags = {
        "cybercrime": "Cybercrime",
        "money_laundering": "Money Laundering",
        "phishing_activities": "Phishing",
        "blacklist_doubt": "Blacklisted",
    }
    any_flag = False
    for key, label in flags.items():
        val = security.get(key, "0")
        if val == "1":
            lines.append(f"  {label}: YES")
            any_flag = True
    if not any_flag:
        lines.append("  No security flags detected")

    is_contract = security.get("contract_address") == "1"
    lines.append(f"  Type: {'Contract' if is_contract else 'EOA (wallet)'}")

    lines.extend([
        "",
        f"Platform Activity (from {len(txns)} recent txns):",
        f"  Four.Meme interactions: {activity['four_meme_txns']}",
        f"  Flap.sh interactions: {activity['flap_sh_txns']}",
        f"  Unique tokens touched: {activity['unique_tokens']}",
    ])

    if txns:
        lines.extend(["", "Recent Transactions:"])
        for i, tx in enumerate(txns[:7], 1):
            h = tx.get("hash", "?")[:16]
            val = int(tx.get("value", "0")) / 1e18
            ago = time_ago(tx.get("timeStamp", ""))
            method = tx.get("functionName", "").split("(")[0] or "transfer"
            lines.append(f"  {i}. {h}... | {method} | {val:.4f} BNB | {ago}")

    if transfers:
        lines.extend(["", "Recent Token Transfers:"])
        for i, tt in enumerate(transfers[:7], 1):
            sym = tt.get("tokenSymbol", "?")
            val = tt.get("value", "0")
            decimals = int(tt.get("tokenDecimal", "18"))
            try:
                amount = int(val) / (10 ** decimals)
                amount_str = f"{amount:,.2f}" if amount < 1e9 else f"{amount:.2e}"
            except (ValueError, ZeroDivisionError):
                amount_str = val
            direction = "IN" if tt.get("to", "").lower() == address.lower() else "OUT"
            ago = time_ago(tt.get("timeStamp", ""))
            lines.append(f"  {i}. {sym} | {direction} {amount_str} | {ago}")

    return "\n".join(lines)


async def main(address: str) -> None:
    if not BSCSCAN_KEY:
        print("WARNING: BSCSCAN_API_KEY not set. BSCScan calls may be rate-limited.\n")

    balance, txns, transfers, security = await asyncio.gather(
        get_bnb_balance(address),
        get_recent_txns(address),
        get_token_transfers(address),
        goplus_address_security(address),
    )

    activity = count_platform_activity(txns, transfers)
    print(format_report(address, balance, txns, transfers, security, activity))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python analyze_wallet.py <wallet_address>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
