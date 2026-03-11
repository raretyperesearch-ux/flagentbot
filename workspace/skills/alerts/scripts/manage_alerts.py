"""Manage alerts — set, list, delete alerts in bot_alerts table."""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import httpx

# ── Supabase helpers ──────────────────────────────────────────────────────
SUPABASE_URL = "https://seartddspffufwiqzwvh.supabase.co/rest/v1"


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


async def _sb_insert(table: str, data: dict) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{SUPABASE_URL}/{table}", headers=_sb_headers(), json=data)
        r.raise_for_status()
        return r.json()


async def _sb_update(table: str, data: dict, filters: dict) -> None:
    params = {k: v for k, v in filters.items()}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.patch(f"{SUPABASE_URL}/{table}", headers=_sb_headers(), json=data, params=params)
        r.raise_for_status()


# ── Commands ──────────────────────────────────────────────────────────────
async def set_alert(telegram_user_id: str, alert_type: str, config_json: str) -> None:
    """Create a new alert."""
    valid_types = ("price_target", "wallet_watch", "volume_spike", "new_token")
    if alert_type not in valid_types:
        print(json.dumps({"status": "error", "message": f"Invalid alert type. Use one of: {', '.join(valid_types)}"}))
        return

    try:
        config = json.loads(config_json)
    except json.JSONDecodeError:
        print(json.dumps({"status": "error", "message": "Invalid JSON config"}))
        return

    row = {
        "telegram_user_id": telegram_user_id,
        "alert_type": alert_type,
        "config": config,
        "active": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    result = await _sb_insert("bot_alerts", row)
    alert_id = result[0].get("id", "?") if result else "?"
    print(json.dumps({
        "status": "success",
        "message": f"Alert set (ID: {alert_id}). I'll message you when it triggers.",
        "alert_id": alert_id,
        "alert_type": alert_type,
    }))


async def list_alerts(telegram_user_id: str) -> None:
    """List all active alerts for a user."""
    rows = await _sb_get("bot_alerts", {
        "telegram_user_id": f"eq.{telegram_user_id}",
        "active": "eq.true",
        "order": "created_at.desc",
    })

    if not rows:
        print(json.dumps({"status": "success", "message": "No active alerts.", "alerts": []}))
        return

    alerts = []
    for r in rows:
        alerts.append({
            "id": r.get("id"),
            "type": r.get("alert_type"),
            "config": r.get("config"),
            "created_at": r.get("created_at"),
            "last_triggered": r.get("last_triggered"),
        })

    lines = ["Your active alerts:\n"]
    for i, a in enumerate(alerts, 1):
        cfg = a["config"] or {}
        if a["type"] == "price_target":
            desc = f"Price of {cfg.get('token', '?')[:10]}... {cfg.get('direction', 'above')} {cfg.get('target_bnb', '?')} BNB"
        elif a["type"] == "wallet_watch":
            desc = f"Watch wallet {cfg.get('wallet', '?')[:10]}..."
        elif a["type"] == "volume_spike":
            desc = f"Volume spike on {cfg.get('platform', '?')}"
        elif a["type"] == "new_token":
            desc = f"New token matching '{cfg.get('keyword', '?')}'"
        else:
            desc = a["type"]
        lines.append(f"  {i}. [{a['id']}] {desc}")

    print(json.dumps({"status": "success", "message": "\n".join(lines), "alerts": alerts}))


async def delete_alert(telegram_user_id: str, alert_id: str) -> None:
    """Deactivate an alert."""
    await _sb_update("bot_alerts", {"active": False}, {
        "id": f"eq.{alert_id}",
        "telegram_user_id": f"eq.{telegram_user_id}",
    })
    print(json.dumps({"status": "success", "message": f"Alert {alert_id} deleted."}))


async def main() -> None:
    if len(sys.argv) < 3:
        print("Usage:")
        print("  manage_alerts.py set <user_id> <alert_type> '<config_json>'")
        print("  manage_alerts.py list <user_id>")
        print("  manage_alerts.py delete <user_id> <alert_id>")
        sys.exit(1)

    action = sys.argv[1]
    user_id = sys.argv[2]

    if action == "set":
        if len(sys.argv) < 5:
            print("Usage: manage_alerts.py set <user_id> <alert_type> '<config_json>'")
            sys.exit(1)
        await set_alert(user_id, sys.argv[3], sys.argv[4])
    elif action == "list":
        await list_alerts(user_id)
    elif action == "delete":
        if len(sys.argv) < 4:
            print("Usage: manage_alerts.py delete <user_id> <alert_id>")
            sys.exit(1)
        await delete_alert(user_id, sys.argv[3])
    else:
        print(f"Unknown action: {action}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
