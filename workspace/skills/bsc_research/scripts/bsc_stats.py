"""Fetch BSC ecosystem stats from Supabase dune_cache and flagent_stats tables."""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import httpx

SUPABASE_URL = "https://seartddspffufwiqzwvh.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def _headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }


async def fetch_dune_cache() -> list[dict]:
    """Fetch all recent dune_cache entries."""
    url = f"{SUPABASE_URL}/rest/v1/dune_cache"
    params = {"select": "*", "order": "updated_at.desc", "limit": "20"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=_headers(), params=params)
        if resp.status_code != 200:
            print(f"dune_cache fetch failed: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
            return []
        return resp.json()


async def fetch_flagent_stats() -> dict | None:
    """Fetch latest flagent_stats snapshot."""
    url = f"{SUPABASE_URL}/rest/v1/flagent_stats"
    params = {"select": "*", "order": "recorded_at.desc", "limit": "1"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=_headers(), params=params)
        if resp.status_code != 200:
            print(f"flagent_stats fetch failed: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
            return None
        rows = resp.json()
        return rows[0] if rows else None


def freshness(ts_str: str | None) -> str:
    """Human-readable freshness indicator."""
    if not ts_str:
        return "unknown"
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts
        mins = int(delta.total_seconds() / 60)
        if mins < 60:
            return f"{mins}m ago"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"
    except (ValueError, TypeError):
        return ts_str


def format_dune_entry(entry: dict) -> str:
    """Format a single dune_cache row."""
    name = entry.get("query_name", "unknown")
    updated = freshness(entry.get("updated_at"))
    result = entry.get("result_json", {})
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            result = {"raw": result[:500]}
    # Compact JSON preview
    preview = json.dumps(result, indent=None, ensure_ascii=False)
    if len(preview) > 300:
        preview = preview[:300] + "..."
    return f"  [{name}] (updated {updated}): {preview}"


async def main() -> None:
    if not SUPABASE_KEY:
        print("ERROR: SUPABASE_SERVICE_KEY not set.")
        sys.exit(1)

    dune_entries, flagent = await asyncio.gather(
        fetch_dune_cache(),
        fetch_flagent_stats(),
    )

    print("=== BSC Ecosystem Data ===\n")

    if dune_entries:
        print(f"Dune Cache ({len(dune_entries)} entries):")
        for entry in dune_entries:
            print(format_dune_entry(entry))
    else:
        print("Dune Cache: No data available")

    print()

    if flagent:
        updated = freshness(flagent.get("recorded_at"))
        print(f"Flagent Platform Stats (as of {updated}):")
        print(f"  Total Users: {flagent.get('total_users', '?')}")
        print(f"  Active (24h): {flagent.get('active_users_24h', '?')}")
        print(f"  Total Trades: {flagent.get('total_trades', '?')}")
        print(f"  Trades (24h): {flagent.get('trades_24h', '?')}")
        vol_total = flagent.get("total_volume_usd")
        vol_24h = flagent.get("volume_24h_usd")
        if vol_total is not None:
            print(f"  Total Volume: ${float(vol_total):,.2f}")
        if vol_24h is not None:
            print(f"  Volume (24h): ${float(vol_24h):,.2f}")
        top = flagent.get("top_traded_tokens")
        if top and isinstance(top, list):
            print("  Top Traded Tokens:")
            for t in top[:5]:
                sym = t.get("symbol", "?")
                tvol = t.get("volume", "?")
                print(f"    {sym}: ${tvol}")
    else:
        print("Flagent Stats: No data available")


if __name__ == "__main__":
    asyncio.run(main())
