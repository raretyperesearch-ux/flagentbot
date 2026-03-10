---
name: bsc-research
description: "BNB Chain ecosystem data — chain health, Four.Meme stats, Flagent performance, and trending tokens. Always available for ecosystem questions."
always: true
metadata: {"nanobot":{"emoji":"🔗"}}
---

# BSC Ecosystem Research

Always-on skill for BNB Chain ecosystem intelligence. Reads cached data from Supabase tables that are populated by external indexers.

## Data sources

Two Supabase tables provide pre-aggregated data:

1. **`dune_cache`** — Cached Dune Analytics query results (chain metrics, DEX volumes, protocol stats)
2. **`flagent_stats`** — Flagent platform performance metrics (users, trades, volume)

Supabase URL: `https://seartddspffufwiqzwvh.supabase.co`
Auth: `SUPABASE_SERVICE_KEY` env var (service role key)

## Reading from Supabase

Use the `exec` tool to run the helper script, or call the REST API directly with `web_fetch`:

### Option A: exec script

```bash
python workspace/skills/bsc_research/scripts/bsc_stats.py
```

### Option B: direct API call with web_fetch

```
GET https://seartddspffufwiqzwvh.supabase.co/rest/v1/dune_cache?select=*&order=updated_at.desc&limit=20
Headers:
  apikey: {SUPABASE_SERVICE_KEY}
  Authorization: Bearer {SUPABASE_SERVICE_KEY}
```

```
GET https://seartddspffufwiqzwvh.supabase.co/rest/v1/flagent_stats?select=*&order=recorded_at.desc&limit=1
Headers:
  apikey: {SUPABASE_SERVICE_KEY}
  Authorization: Bearer {SUPABASE_SERVICE_KEY}
```

## dune_cache table schema

| Column | Type | Description |
|--------|------|-------------|
| `id` | uuid | Primary key |
| `query_name` | text | Identifier (e.g. `bsc_daily_txns`, `four_meme_volume`, `top_tokens_24h`) |
| `result_json` | jsonb | Full query result as JSON |
| `updated_at` | timestamp | When this cache entry was last refreshed |

### Known query_name values

- `bsc_daily_txns` — Daily transaction count and gas stats
- `bsc_active_addresses` — Daily active addresses
- `four_meme_volume` — Four.Meme 24h trading volume and launch count
- `four_meme_top_tokens` — Top Four.Meme tokens by volume (last 24h)
- `pancakeswap_volume` — PancakeSwap v3 BSC volume
- `bsc_gas_stats` — Gas price percentiles
- `top_tokens_24h` — Top BSC tokens by volume across all DEXes

## flagent_stats table schema

| Column | Type | Description |
|--------|------|-------------|
| `id` | uuid | Primary key |
| `total_users` | int | Total registered Flagent users |
| `active_users_24h` | int | Users active in last 24 hours |
| `total_trades` | int | Cumulative trades executed |
| `trades_24h` | int | Trades in last 24 hours |
| `total_volume_usd` | numeric | Cumulative volume in USD |
| `volume_24h_usd` | numeric | 24h volume in USD |
| `top_traded_tokens` | jsonb | Array of {address, symbol, volume} |
| `recorded_at` | timestamp | Snapshot timestamp |

## When to use

This skill's data is always in context. Reference it when the user asks:
- "How's BSC doing?" / "chain health" / "network stats"
- "What's trending on Four.Meme?" / "top tokens today"
- "How is Flagent performing?" / "Flagent stats"
- "Gas prices on BSC?"
- Any general BSC ecosystem question

## Response format

Present data clearly with context:

```
BSC Network (as of {updated_at}):
  Daily Transactions: {count}
  Active Addresses: {count}
  Gas: {low} / {median} / {high} gwei

Four.Meme (24h):
  Volume: ${volume}
  New Launches: {count}
  Top Token: {symbol} (${volume})

Flagent Platform:
  Users: {total} ({active_24h} active today)
  Trades: {trades_24h} today (${volume_24h})
```

## Freshness

Data is cached and may be 15-60 minutes old. Always show the `updated_at` / `recorded_at` timestamp so the user knows how fresh the data is. If data is more than 2 hours old, mention that it may be stale.

## Notes

- If tables are empty or the query fails, tell the user that ecosystem data is temporarily unavailable.
- Do NOT make up numbers. Only report what the tables contain.
- The `result_json` field in `dune_cache` contains the raw Dune query output — parse the relevant fields based on the `query_name`.
