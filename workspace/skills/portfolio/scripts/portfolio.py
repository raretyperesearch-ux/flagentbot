"""Portfolio viewer — BNB balance, token positions, PnL, trade history."""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import httpx

# ── Constants ──────────────────────────────────────────────────────────────
BSCSCAN_KEY = os.environ.get("BSCSCAN_API_KEY", "")
BSCSCAN = "https://api.bscscan.com/api"
SUPABASE_URL = "https://seartddspffufwiqzwvh.supabase.co/rest/v1"


# ── Supabase helpers ──────────────────────────────────────────────────────
def _sb_headers() -> dict:
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


async def _sb_get(table: str, params: dict) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{SUPABASE_URL}/{table}", headers=_sb_headers(), params=params)
        r.raise_for_status()
        return r.json()


# ── BSCScan helpers ───────────────────────────────────────────────────────
async def _bscscan(params: dict) -> dict:
    params["apikey"] = BSCSCAN_KEY
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(BSCSCAN, params=params)
        r.raise_for_status()
        return r.json()


async def get_bnb_balance(address: str) -> float:
    data = await _bscscan({"module": "account", "action": "balance", "address": address, "tag": "latest"})
    wei = int(data.get("result", "0"))
    return wei / 1e18


async def get_token_balances(address: str) -> list[dict]:
    """Get recent token transfers to find tokens the wallet holds."""
    data = await _bscscan({
        "module": "account", "action": "tokentx", "address": address,
        "startblock": 0, "endblock": 99999999,
        "page": 1, "offset": 50, "sort": "desc",
    })
    result = data.get("result", [])
    return result if isinstance(result, list) else []


# ── Main logic ─────────────────────────────────────────────────────────────
def time_ago(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = (datetime.now(timezone.utc) - dt).total_seconds()
        if delta < 3600:
            return f"{int(delta / 60)}m ago"
        if delta < 86400:
            return f"{int(delta / 3600)}h ago"
        return f"{int(delta / 86400)}d ago"
    except (ValueError, TypeError):
        return "?"


def format_report(
    address: str, bnb_balance: float,
    positions: list[dict], token_summary: dict,
) -> str:
    lines = [
        f"Portfolio for {address}",
        f"BNB Balance: {bnb_balance:.4f} BNB",
    ]

    # Aggregate positions by token
    buys: dict[str, dict] = {}
    sells: dict[str, dict] = {}
    for p in positions:
        token = p.get("token_address", "").lower()
        side = p.get("side", "")
        bnb = float(p.get("bnb_amount", 0) or 0)
        if side == "buy":
            if token not in buys:
                buys[token] = {"total_bnb": 0, "count": 0}
            buys[token]["total_bnb"] += bnb
            buys[token]["count"] += 1
        elif side == "sell":
            if token not in sells:
                sells[token] = {"total_bnb": 0, "count": 0}
            sells[token]["total_bnb"] += bnb
            sells[token]["count"] += 1

    # Open positions (bought but not fully sold)
    open_tokens = set(buys.keys()) - set(sells.keys())
    partially_sold = set(buys.keys()) & set(sells.keys())
    all_active = open_tokens | partially_sold

    if all_active:
        lines.extend(["", "Positions:"])
        for i, token in enumerate(sorted(all_active), 1):
            sym = token_summary.get(token, {}).get("symbol", token[:10] + "...")
            bought_bnb = buys.get(token, {}).get("total_bnb", 0)
            sold_bnb = sells.get(token, {}).get("total_bnb", 0)
            net = bought_bnb - sold_bnb
            status = "open" if token in open_tokens else "partial"
            lines.append(f"  {i}. {sym} | Invested: {bought_bnb:.4f} BNB | Sold: {sold_bnb:.4f} BNB | Net: {net:.4f} BNB [{status}]")

    # Recent trades
    recent = sorted(positions, key=lambda p: p.get("created_at", ""), reverse=True)[:10]
    if recent:
        lines.extend(["", "Recent Trades:"])
        for i, p in enumerate(recent, 1):
            side = p.get("side", "?").upper()
            token = p.get("token_address", "?")
            sym = token_summary.get(token.lower(), {}).get("symbol", token[:10] + "...")
            bnb = float(p.get("bnb_amount", 0) or 0)
            platform = p.get("platform", "?")
            tx = p.get("tx_hash", "?")[:16]
            ago = time_ago(p.get("created_at", ""))
            lines.append(f"  {i}. {side} {sym} | {bnb:.4f} BNB | {platform} | {ago} | {tx}...")

    # Summary stats
    total_bought = sum(b["total_bnb"] for b in buys.values())
    total_sold = sum(s["total_bnb"] for s in sells.values())
    lines.extend([
        "",
        f"Total Invested: {total_bought:.4f} BNB",
        f"Total Sold: {total_sold:.4f} BNB",
        f"Total Trades: {len(positions)}",
    ])

    return "\n".join(lines)


async def main(telegram_user_id: str) -> None:
    # 1. Get user wallet address
    users = await _sb_get("bot_users", {
        "telegram_user_id": f"eq.{telegram_user_id}",
        "select": "wallet_address",
    })
    if not users:
        print("No wallet found. Run /setup first.")
        return

    address = users[0]["wallet_address"]

    # 2. Fetch data in parallel
    bnb_task = asyncio.create_task(get_bnb_balance(address))
    positions_task = asyncio.create_task(_sb_get("bot_positions", {
        "telegram_user_id": f"eq.{telegram_user_id}",
        "order": "created_at.desc",
    }))
    transfers_task = asyncio.create_task(get_token_balances(address))

    bnb_balance = await bnb_task
    positions = await positions_task
    transfers = await transfers_task

    # Build token symbol lookup from transfers
    token_summary: dict[str, dict] = {}
    for t in transfers:
        ca = (t.get("contractAddress") or "").lower()
        if ca and ca not in token_summary:
            token_summary[ca] = {"symbol": t.get("tokenSymbol", "?")}

    if not BSCSCAN_KEY:
        print("WARNING: BSCSCAN_API_KEY not set. Balance queries may be rate-limited.\n")

    print(format_report(address, bnb_balance, positions, token_summary))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python portfolio.py <telegram_user_id>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
