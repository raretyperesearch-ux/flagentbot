"""Token analysis script — combines GoPlus security + Four.Meme bonding curve data."""

import asyncio
import json
import sys

import httpx

BSC_RPC = "https://bsc-dataseed1.binance.org"
TOKEN_MANAGER_HELPER = "0xF251F83e40a78868FcfA3FA4599Dad6494E46034"


async def goplus_security(address: str) -> dict:
    """Fetch GoPlus token security report for BSC."""
    url = f"https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses={address.lower()}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
    return data.get("result", {}).get(address.lower(), {})


async def four_meme_info(token_address: str) -> dict:
    """Call TokenManagerHelper3.getTokenInfo(address) via eth_call."""
    padded = token_address.lower().replace("0x", "").zfill(64)
    calldata = "0x1a7a98e2" + padded
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": TOKEN_MANAGER_HELPER, "data": calldata}, "latest"],
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(BSC_RPC, json=payload)
        result = resp.json().get("result", "0x")

    if result == "0x" or len(result) < 66:
        return {"on_four_meme": False}

    hex_data = result[2:]
    chunks = [hex_data[i:i + 64] for i in range(0, len(hex_data), 64)]
    if len(chunks) < 12:
        return {"on_four_meme": False}

    # Decode getTokenInfo struct (12 fields)
    version = int(chunks[0], 16)
    token_manager = "0x" + chunks[1][-40:]
    quote = "0x" + chunks[2][-40:]
    last_price = int(chunks[3], 16)
    offers = int(chunks[7], 16)
    max_offers = int(chunks[8], 16)
    funds_wei = int(chunks[9], 16)
    max_funds_wei = int(chunks[10], 16)
    liquidity_added = int(chunks[11], 16) != 0

    progress = 100 - (offers * 100 // max_offers) if max_offers > 0 else 0
    on_curve = not liquidity_added and quote == "0x" + "0" * 40

    return {
        "on_four_meme": True,
        "on_curve": on_curve,
        "bonding_progress": progress,
        "funds_bnb": funds_wei / 1e18,
        "max_funds_bnb": max_funds_wei / 1e18,
        "liquidity_added": liquidity_added,
        "last_price_wei": last_price,
        "version": version,
    }


def format_tax(val: str) -> str:
    """Convert GoPlus tax string (e.g. '0.05') to percentage."""
    try:
        return f"{float(val) * 100:.1f}%"
    except (ValueError, TypeError):
        return "unknown"


def format_report(address: str, gp: dict, fm: dict) -> str:
    """Format combined analysis report."""
    name = gp.get("token_name", "Unknown")
    symbol = gp.get("token_symbol", "???")
    holders = gp.get("holder_count", "?")

    honeypot = "YES — CANNOT SELL" if gp.get("is_honeypot") == "1" else "No"
    buy_tax = format_tax(gp.get("buy_tax", ""))
    sell_tax = format_tax(gp.get("sell_tax", ""))
    open_src = "Yes" if gp.get("is_open_source") == "1" else "No"
    mintable = "Yes" if gp.get("is_mintable") == "1" else "No"
    proxy = "Yes" if gp.get("is_proxy") == "1" else "No"
    owner = gp.get("owner_address", "unknown")

    lines = [
        f"Token: {name} ({symbol})",
        f"Contract: {address}",
        f"Holders: {holders}",
        "",
        "Security:",
        f"  Honeypot: {honeypot}",
        f"  Buy Tax: {buy_tax} | Sell Tax: {sell_tax}",
        f"  Open Source: {open_src}",
        f"  Mintable: {mintable}",
        f"  Proxy: {proxy}",
        f"  Owner: {owner}",
        "",
    ]

    # Risk flags
    warnings = []
    if gp.get("is_honeypot") == "1":
        warnings.append("CRITICAL: Honeypot detected — tokens cannot be sold")
    try:
        if float(gp.get("sell_tax", "0")) > 0.10:
            warnings.append(f"HIGH: Sell tax > 10% ({sell_tax})")
    except ValueError:
        pass
    if gp.get("is_mintable") == "1":
        warnings.append("WARN: Owner can mint new tokens (inflationary)")
    if gp.get("is_proxy") == "1":
        warnings.append("WARN: Proxy contract — code can be changed")
    if gp.get("is_open_source") == "0":
        warnings.append("WARN: Source code not verified on BSCScan")

    # Check top holder concentration
    for h in (gp.get("holders") or [])[:3]:
        try:
            pct = float(h.get("percent", 0)) * 100
            if pct > 20 and h.get("is_contract") != 1:
                warnings.append(f"WARN: Whale {h['address'][:10]}... holds {pct:.1f}%")
        except (ValueError, TypeError):
            pass

    if warnings:
        lines.append("Risk Flags:")
        for w in warnings:
            lines.append(f"  {w}")
        lines.append("")

    # Four.Meme status
    if fm.get("on_four_meme"):
        if fm.get("on_curve"):
            lines.append(f"Four.Meme: On bonding curve ({fm['bonding_progress']}% filled)")
            lines.append(f"  Raised: {fm['funds_bnb']:.4f} / {fm['max_funds_bnb']:.4f} BNB")
        elif fm.get("liquidity_added"):
            lines.append("Four.Meme: Graduated (liquidity added to PancakeSwap)")
        else:
            lines.append("Four.Meme: Detected (curve state unclear)")
    else:
        lines.append("Four.Meme: Not found (may be non-Four.Meme token)")

    # Top holders
    top_holders = (gp.get("holders") or [])[:5]
    if top_holders:
        lines.append("")
        lines.append("Top Holders:")
        for i, h in enumerate(top_holders, 1):
            addr = h.get("address", "?")
            pct = "?"
            try:
                pct = f"{float(h.get('percent', 0)) * 100:.2f}%"
            except (ValueError, TypeError):
                pass
            locked = " (locked)" if h.get("is_locked") == 1 else ""
            contract = " [contract]" if h.get("is_contract") == 1 else ""
            lines.append(f"  {i}. {addr[:16]}... — {pct}{locked}{contract}")

    return "\n".join(lines)


async def main(address: str) -> None:
    gp_task = asyncio.create_task(goplus_security(address))
    fm_task = asyncio.create_task(four_meme_info(address))
    gp = await gp_task
    fm = await fm_task

    if not gp:
        print(f"No GoPlus data found for {address}. Token may be too new or invalid.")
        return

    print(format_report(address, gp, fm))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python analyze_token.py <contract_address>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
