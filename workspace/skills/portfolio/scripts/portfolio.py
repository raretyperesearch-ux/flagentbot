"""Portfolio viewer — BNB balance, token positions, live PnL, trade history."""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from web3 import Web3

# Add shared skills to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "shared"))
from price_engine import get_token_price, _get_bnb_price_usd

# ── Constants ──────────────────────────────────────────────────────────────
BSC_RPC = "https://bsc-dataseed.binance.org"
BSCSCAN_KEY = os.environ.get("BSCSCAN_API_KEY", "")
BSCSCAN = "https://api.bscscan.com/api"
SUPABASE_URL = "https://seartddspffufwiqzwvh.supabase.co/rest/v1"

ERC20_BALANCE_ABI = [{
    "name": "balanceOf",
    "type": "function",
    "stateMutability": "view",
    "inputs": [{"name": "account", "type": "address"}],
    "outputs": [{"name": "", "type": "uint256"}],
}, {
    "name": "decimals",
    "type": "function",
    "stateMutability": "view",
    "inputs": [],
    "outputs": [{"name": "", "type": "uint8"}],
}, {
    "name": "symbol",
    "type": "function",
    "stateMutability": "view",
    "inputs": [],
    "outputs": [{"name": "", "type": "string"}],
}]


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


# ── On-chain token balance ───────────────────────────────────────────────
def _get_token_balance(w3: Web3, token_address: str, wallet: str) -> tuple[int, int, str]:
    """Returns (raw_balance, decimals, symbol)."""
    token = Web3.to_checksum_address(token_address)
    erc20 = w3.eth.contract(address=token, abi=ERC20_BALANCE_ABI)
    balance = erc20.functions.balanceOf(Web3.to_checksum_address(wallet)).call()
    decimals = 18
    symbol = "?"
    try:
        decimals = erc20.functions.decimals().call()
    except Exception:
        pass
    try:
        symbol = erc20.functions.symbol().call()
    except Exception:
        pass
    return balance, decimals, symbol


# ── Helpers ───────────────────────────────────────────────────────────────
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


async def build_portfolio(address: str, positions: list[dict]) -> str:
    w3 = Web3(Web3.HTTPProvider(BSC_RPC))

    # Aggregate positions by token
    buys: dict[str, float] = {}   # token -> total BNB invested
    sells: dict[str, float] = {}  # token -> total BNB received
    for p in positions:
        token = (p.get("token_address") or "").lower()
        side = p.get("side", "")
        bnb = float(p.get("cost_bnb", 0) or 0)
        if side == "buy":
            buys[token] = buys.get(token, 0) + bnb
        elif side == "sell":
            sells[token] = sells.get(token, 0) + bnb

    active_tokens = set(buys.keys())

    # Fetch BNB balance + BNB/USD price in parallel
    bnb_task = asyncio.create_task(get_bnb_balance(address))
    bnb_usd_task = asyncio.create_task(_get_bnb_price_usd())

    # Fetch live prices for all active tokens
    price_tasks = {t: asyncio.create_task(get_token_price(t)) for t in active_tokens}

    bnb_balance = await bnb_task
    bnb_usd = await bnb_usd_task or 0
    prices = {t: await task for t, task in price_tasks.items()}

    # Build position lines with live PnL
    lines = [
        f"Portfolio for {address[:8]}...{address[-6:]}",
        f"BNB: {bnb_balance:.4f} (~${bnb_balance * bnb_usd:,.2f})",
    ]

    total_invested = 0.0
    total_current_value = 0.0
    position_lines = []

    for i, token in enumerate(sorted(active_tokens), 1):
        invested_bnb = buys.get(token, 0)
        sold_bnb = sells.get(token, 0)
        total_invested += invested_bnb

        # Get on-chain balance
        try:
            raw_bal, decimals, symbol = _get_token_balance(w3, token, address)
            human_bal = raw_bal / (10 ** decimals)
        except Exception:
            raw_bal, decimals, symbol, human_bal = 0, 18, "?", 0

        price_data = prices.get(token, {})
        price_bnb = price_data.get("price_bnb", 0)
        price_usd = price_data.get("price_usd", 0)
        source = price_data.get("source", "unknown")

        # Current value = on-chain balance * price
        holding_value_bnb = human_bal * price_bnb if price_bnb > 0 else 0
        holding_value_usd = human_bal * price_usd if price_usd > 0 else 0
        total_current_value += holding_value_bnb

        # PnL = current holding value + sold BNB - invested BNB
        net_bnb = holding_value_bnb + sold_bnb - invested_bnb
        pnl_pct = (net_bnb / invested_bnb * 100) if invested_bnb > 0 else 0

        pnl_sign = "+" if net_bnb >= 0 else ""
        pnl_str = f"{pnl_sign}{pnl_pct:.1f}%"

        if human_bal > 0:
            position_lines.append(
                f"  {i}. {symbol} | {human_bal:,.0f} tokens"
                f" | Worth: {holding_value_bnb:.4f} BNB (~${holding_value_usd:,.2f})"
                f" | Cost: {invested_bnb:.4f} BNB | PnL: {pnl_str}"
                f" [{source}]"
            )
        else:
            # Fully sold
            net = sold_bnb - invested_bnb
            sign = "+" if net >= 0 else ""
            position_lines.append(
                f"  {i}. {symbol} | CLOSED"
                f" | Invested: {invested_bnb:.4f} BNB | Returned: {sold_bnb:.4f} BNB"
                f" | PnL: {sign}{net:.4f} BNB ({pnl_str})"
            )

    if position_lines:
        lines.extend(["", "Positions:"])
        lines.extend(position_lines)

    # Recent trades
    recent = sorted(positions, key=lambda p: p.get("created_at", ""), reverse=True)[:10]
    if recent:
        lines.extend(["", "Recent Trades:"])
        for i, p in enumerate(recent, 1):
            side = p.get("side", "?").upper()
            token = (p.get("token_address") or "?").lower()
            # Try to get symbol from price data
            sym = "?"
            try:
                _, _, sym = _get_token_balance(w3, token, address)
            except Exception:
                pass
            bnb = float(p.get("cost_bnb", 0) or 0)
            platform = p.get("platform", "?")
            tx = (p.get("tx_hash_buy") or p.get("tx_hash_sell") or "?")[:16]
            ago = time_ago(p.get("created_at", ""))
            lines.append(f"  {i}. {side} {sym} | {bnb:.4f} BNB | {platform} | {ago} | {tx}...")

    # Summary
    total_sold = sum(sells.values())
    net_pnl_bnb = total_current_value + total_sold - total_invested
    net_pnl_pct = (net_pnl_bnb / total_invested * 100) if total_invested > 0 else 0
    sign = "+" if net_pnl_bnb >= 0 else ""

    lines.extend([
        "",
        f"Total Invested: {total_invested:.4f} BNB",
        f"Holdings Value: {total_current_value:.4f} BNB (~${total_current_value * bnb_usd:,.2f})",
        f"Total Sold: {total_sold:.4f} BNB",
        f"Net PnL: {sign}{net_pnl_bnb:.4f} BNB ({sign}{net_pnl_pct:.1f}%)",
        f"Total Trades: {len(positions)}",
    ])

    if not BSCSCAN_KEY:
        lines.insert(0, "WARNING: BSCSCAN_API_KEY not set. Balance queries may be rate-limited.\n")

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

    # 2. Fetch positions
    positions = await _sb_get("bot_positions", {
        "telegram_user_id": f"eq.{telegram_user_id}",
        "order": "created_at.desc",
    })

    if not positions:
        print("No open positions yet. When you make trades through me, they'll show up here.")
        return

    # 3. Build and print report
    report = await build_portfolio(address, positions)
    print(report)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python portfolio.py <telegram_user_id>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
