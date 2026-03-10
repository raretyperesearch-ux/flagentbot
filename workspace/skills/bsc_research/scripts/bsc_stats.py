"""Fetch BSC ecosystem stats from Supabase dune_cache (fast) with Dune API fallback."""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import httpx

SUPABASE_URL = "https://seartddspffufwiqzwvh.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
DUNE_API_KEY = os.environ.get("DUNE_API_KEY", "")

# Dune query IDs from env vars
DUNE_QUERIES = {
    "bsc_health": os.environ.get("DUNE_QUERY_BSC_HEALTH", ""),
    "fourmeme": os.environ.get("DUNE_QUERY_FOURMEME", ""),
    "smart_money": os.environ.get("DUNE_QUERY_SMART_MONEY", ""),
}

STALE_MINUTES = 30  # Cache entries older than this trigger a Dune API refresh


def _sb_headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def fetch_dune_cache() -> list[dict]:
    """Fetch all recent dune_cache entries."""
    url = f"{SUPABASE_URL}/rest/v1/dune_cache"
    params = {"select": "*", "order": "updated_at.desc", "limit": "20"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=_sb_headers(), params=params)
        if resp.status_code != 200:
            print(f"dune_cache fetch failed: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
            return []
        return resp.json()


async def fetch_flagent_stats() -> dict | None:
    """Fetch latest flagent_stats snapshot."""
    url = f"{SUPABASE_URL}/rest/v1/flagent_stats"
    params = {"select": "*", "order": "recorded_at.desc", "limit": "1"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=_sb_headers(), params=params)
        if resp.status_code != 200:
            print(f"flagent_stats fetch failed: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
            return None
        rows = resp.json()
        return rows[0] if rows else None


async def fetch_dune_direct(query_name: str, query_id: str) -> dict | None:
    """Call Dune API directly for fresh results."""
    if not DUNE_API_KEY or not query_id:
        return None
    url = f"https://api.dune.com/api/v1/query/{query_id}/results"
    headers = {"x-dune-api-key": DUNE_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                print(f"Dune API error for {query_name}: {resp.status_code}", file=sys.stderr)
                return None
            data = resp.json()
            result = data.get("result", {})
            rows = result.get("rows", [])
            return {"query_name": query_name, "rows": rows, "source": "dune_api"}
    except Exception as e:
        print(f"Dune API call failed for {query_name}: {e}", file=sys.stderr)
        return None


async def upsert_dune_cache(query_name: str, result_json: dict) -> None:
    """Update Supabase dune_cache with fresh Dune results."""
    if not SUPABASE_KEY:
        return
    url = f"{SUPABASE_URL}/rest/v1/dune_cache"
    headers = _sb_headers()
    headers["Prefer"] = "return=representation,resolution=merge-duplicates"
    data = {
        "query_name": query_name,
        "result_json": result_json,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(url, headers=headers, json=data, params={"on_conflict": "query_name"})
    except Exception as e:
        print(f"Failed to update dune_cache for {query_name}: {e}", file=sys.stderr)


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


def is_stale(ts_str: str | None) -> bool:
    """Check if a timestamp is older than STALE_MINUTES."""
    if not ts_str:
        return True
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts
        return delta.total_seconds() > STALE_MINUTES * 60
    except (ValueError, TypeError):
        return True


def format_dune_entry(entry: dict, source: str = "cache") -> str:
    """Format a single dune_cache row."""
    name = entry.get("query_name", "unknown")
    updated = freshness(entry.get("updated_at"))
    result = entry.get("result_json", {})
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            result = {"raw": result[:500]}
    preview = json.dumps(result, indent=None, ensure_ascii=False)
    if len(preview) > 300:
        preview = preview[:300] + "..."
    src_tag = f" [{source}]" if source != "cache" else ""
    return f"  [{name}] (updated {updated}{src_tag}): {preview}"


async def main() -> None:
    if not SUPABASE_KEY:
        print("ERROR: SUPABASE_SERVICE_KEY not set.")
        sys.exit(1)

    dune_entries, flagent = await asyncio.gather(
        fetch_dune_cache(),
        fetch_flagent_stats(),
    )

    # Build lookup of cached entries by query_name
    cache_by_name: dict[str, dict] = {}
    for entry in dune_entries:
        qname = entry.get("query_name", "")
        if qname:
            cache_by_name[qname] = entry

    # Check for stale/missing entries and refresh from Dune API
    refreshed: dict[str, dict] = {}
    refresh_tasks = []
    for qname, qid in DUNE_QUERIES.items():
        if not qid:
            continue
        cached = cache_by_name.get(qname)
        if cached and not is_stale(cached.get("updated_at")):
            continue  # Cache is fresh
        refresh_tasks.append((qname, qid))

    if refresh_tasks and DUNE_API_KEY:
        results = await asyncio.gather(
            *[fetch_dune_direct(qname, qid) for qname, qid in refresh_tasks]
        )
        for result in results:
            if result and result.get("rows"):
                qname = result["query_name"]
                refreshed[qname] = result
                # Update cache in background
                await upsert_dune_cache(qname, result["rows"])
                # Update local view
                cache_by_name[qname] = {
                    "query_name": qname,
                    "result_json": result["rows"],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }

    print("=== BSC Ecosystem Data ===\n")

    all_entries = list(cache_by_name.values())
    if all_entries:
        print(f"Dune Data ({len(all_entries)} queries):")
        for entry in all_entries:
            source = "dune_api" if entry.get("query_name") in refreshed else "cache"
            print(format_dune_entry(entry, source))
    else:
        print("Dune Data: No data available")

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
