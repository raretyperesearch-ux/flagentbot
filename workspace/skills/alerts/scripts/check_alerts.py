"""Check all active alerts and output triggered ones for delivery."""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Add shared skills to path for price_engine
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "shared"))

# ── Supabase helpers ──────────────────────────────────────────────────────
SUPABASE_URL = "https://seartddspffufwiqzwvh.supabase.co/rest/v1"
BSCSCAN = "https://api.bscscan.com/api"
BSCSCAN_KEY = os.environ.get("BSCSCAN_API_KEY", "")


def _sb_headers() -> dict:
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def _sb_get(table: str, params: dict) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{SUPABASE_URL}/{table}", headers=_sb_headers(), params=params)
        r.raise_for_status()
        return r.json()


async def _sb_update(table: str, data: dict, filters: dict) -> None:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.patch(f"{SUPABASE_URL}/{table}", headers=_sb_headers(), json=data, params=filters)
        r.raise_for_status()


# ── Alert checkers ────────────────────────────────────────────────────────

async def check_wallet_watch(alert: dict) -> dict | None:
    """Check if a watched wallet has new transactions."""
    config = alert.get("config", {})
    wallet = config.get("wallet", "")
    last_tx = config.get("last_tx_hash", "")

    if not wallet:
        return None

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(BSCSCAN, params={
                "module": "account", "action": "txlist",
                "address": wallet, "startblock": 0, "endblock": 99999999,
                "page": 1, "offset": 5, "sort": "desc",
                "apikey": BSCSCAN_KEY,
            })
            data = r.json()
            txs = data.get("result", [])
            if not isinstance(txs, list) or not txs:
                return None

            latest = txs[0]
            latest_hash = latest.get("hash", "")

            if latest_hash and latest_hash != last_tx:
                # Update stored last_tx_hash
                new_config = {**config, "last_tx_hash": latest_hash}
                await _sb_update("bot_alerts", {
                    "config": new_config,
                    "last_triggered": datetime.now(timezone.utc).isoformat(),
                }, {"id": f"eq.{alert['id']}"})

                value_eth = int(latest.get("value", "0")) / 1e18
                to_addr = latest.get("to", "?")
                return {
                    "alert_id": alert["id"],
                    "telegram_user_id": alert["user_id"],
                    "type": "wallet_watch",
                    "message": (
                        f"Wallet activity detected!\n"
                        f"Wallet: {wallet[:10]}...\n"
                        f"New tx: {latest_hash[:16]}...\n"
                        f"To: {to_addr[:10]}...\n"
                        f"Value: {value_eth:.4f} BNB"
                    ),
                }
    except Exception:
        pass
    return None


async def check_price_target(alert: dict) -> dict | None:
    """Check if a token has crossed a price target."""
    config = alert.get("config", {})
    token = config.get("token", "")
    target = float(config.get("target_bnb", 0))
    direction = config.get("direction", "above")

    if not token or target <= 0:
        return None

    try:
        from price_engine import get_token_price
        price_data = await get_token_price(token)
        price_bnb = price_data.get("price_bnb", 0)

        if price_bnb <= 0:
            return None

        triggered = False
        if direction == "above" and price_bnb >= target:
            triggered = True
        elif direction == "below" and price_bnb <= target:
            triggered = True

        if triggered:
            await _sb_update("bot_alerts", {
                "last_triggered": datetime.now(timezone.utc).isoformat(),
                "is_active": False,  # One-shot: deactivate after trigger
            }, {"id": f"eq.{alert['id']}"})

            price_usd = price_data.get("price_usd", 0)
            return {
                "alert_id": alert["id"],
                "telegram_user_id": alert["user_id"],
                "type": "price_target",
                "message": (
                    f"Price alert triggered!\n"
                    f"Token: {token[:10]}...\n"
                    f"Price: {price_bnb:.8f} BNB (~${price_usd:,.6f})\n"
                    f"Target: {direction} {target} BNB\n"
                    f"Source: {price_data.get('source', '?')}"
                ),
            }
    except Exception:
        pass
    return None


async def check_volume_spike(alert: dict) -> dict | None:
    """Check if volume has spiked above 2x baseline."""
    config = alert.get("config", {})
    platform = config.get("platform", "")
    baseline = float(config.get("baseline_volume", 0))

    if baseline <= 0:
        return None

    try:
        # Use DexScreener for volume data
        # For platform-level volume, check a known reference token
        async with httpx.AsyncClient(timeout=10) as c:
            # Check dune_cache for fresh volume data
            rows = await _sb_get("dune_cache", {
                "query_name": f"eq.{platform}_volume",
                "select": "data",
                "limit": "1",
            })
            if not rows:
                return None

            data = rows[0].get("data", {})
            current_volume = float(data.get("volume_24h", 0))

            if current_volume > baseline * 2:
                # Update baseline to current volume
                new_config = {**config, "baseline_volume": current_volume}
                await _sb_update("bot_alerts", {
                    "config": new_config,
                    "last_triggered": datetime.now(timezone.utc).isoformat(),
                }, {"id": f"eq.{alert['id']}"})

                spike_pct = ((current_volume / baseline) - 1) * 100
                return {
                    "alert_id": alert["id"],
                    "telegram_user_id": alert["user_id"],
                    "type": "volume_spike",
                    "message": (
                        f"Volume spike detected!\n"
                        f"Platform: {platform}\n"
                        f"Volume: ${current_volume:,.0f} (was ${baseline:,.0f})\n"
                        f"Spike: +{spike_pct:.0f}%"
                    ),
                }
    except Exception:
        pass
    return None


async def check_new_token(alert: dict) -> dict | None:
    """Check if a new token matching a keyword has been created."""
    config = alert.get("config", {})
    keyword = config.get("keyword", "").lower()

    if not keyword:
        return None

    try:
        # Check dune_cache for recent token launches
        rows = await _sb_get("dune_cache", {
            "query_name": "eq.recent_launches",
            "select": "data",
            "limit": "1",
        })
        if not rows:
            return None

        data = rows[0].get("data", {})
        tokens = data.get("tokens", [])

        # Track seen tokens to avoid re-alerting
        seen = set(config.get("seen_tokens", []))
        new_matches = []

        for t in tokens:
            name = (t.get("name") or "").lower()
            symbol = (t.get("symbol") or "").lower()
            address = t.get("address", "")
            if address in seen:
                continue
            if keyword in name or keyword in symbol:
                new_matches.append(t)
                seen.add(address)

        if new_matches:
            new_config = {**config, "seen_tokens": list(seen)[-50:]}  # Keep last 50
            await _sb_update("bot_alerts", {
                "config": new_config,
                "last_triggered": datetime.now(timezone.utc).isoformat(),
            }, {"id": f"eq.{alert['id']}"})

            lines = [f"New token(s) matching '{keyword}' found!\n"]
            for t in new_matches[:3]:
                lines.append(f"  {t.get('name', '?')} ({t.get('symbol', '?')}): {t.get('address', '?')}")

            return {
                "alert_id": alert["id"],
                "telegram_user_id": alert["user_id"],
                "type": "new_token",
                "message": "\n".join(lines),
            }
    except Exception:
        pass
    return None


# ── Main ──────────────────────────────────────────────────────────────────

CHECKERS = {
    "wallet_watch": check_wallet_watch,
    "price_target": check_price_target,
    "volume_spike": check_volume_spike,
    "new_token": check_new_token,
}


async def main() -> None:
    """Check all active alerts and output triggered ones."""
    try:
        alerts = await _sb_get("bot_alerts", {
            "is_active": "eq.true",
            "order": "created_at.asc",
        })
    except Exception as e:
        print(json.dumps({"triggered": [], "checked": 0, "error": f"Failed to fetch alerts: {e}"}))
        return

    if not alerts:
        print(json.dumps({"triggered": [], "checked": 0}))
        return

    triggered = []
    errors = []
    for alert in alerts:
        alert_type = alert.get("alert_type", "")
        checker = CHECKERS.get(alert_type)
        if not checker:
            continue

        try:
            result = await checker(alert)
            if result:
                triggered.append(result)
        except Exception as e:
            errors.append({"alert_id": alert.get("id"), "type": alert_type, "error": str(e)[:200]})

    output = {
        "triggered": triggered,
        "checked": len(alerts),
    }
    if errors:
        output["errors"] = errors
    print(json.dumps(output))


if __name__ == "__main__":
    asyncio.run(main())
