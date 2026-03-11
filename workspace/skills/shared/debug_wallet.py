#!/usr/bin/env python3
import os, sys, json
try:
    import httpx
except ImportError:
    print("httpx not installed")
    sys.exit(1)

user_id = sys.argv[1] if len(sys.argv) > 1 else "6850147865"

sb_url = os.environ.get("SUPABASE_URL", "https://seartddspffufwiqzwvh.supabase.co")
sb_key = os.environ.get("SUPABASE_SERVICE_KEY", "")

print(f"=== WALLET DEBUG ===")
print(f"telegram_user_id: {user_id}")
print(f"SUPABASE_URL: {sb_url}")
print(f"SUPABASE_SERVICE_KEY set: {bool(sb_key)}")
print(f"SUPABASE_SERVICE_KEY length: {len(sb_key)}")
print(f"SUPABASE_SERVICE_KEY first 10 chars: {sb_key[:10] if sb_key else 'EMPTY'}")
print(f"ENCRYPTION_KEY set: {bool(os.environ.get('ENCRYPTION_KEY'))}")

if not sb_key:
    print("ERROR: SUPABASE_SERVICE_KEY is empty!")
    sys.exit(1)

url = f"{sb_url}/rest/v1/bot_users?telegram_user_id=eq.{user_id}&select=wallet_address,encrypted_private_key"
headers = {
    "apikey": sb_key,
    "Authorization": f"Bearer {sb_key}",
}

print(f"\nURL: {url}")
print(f"Headers: apikey={sb_key[:10]}..., Authorization=Bearer {sb_key[:10]}...")

resp = httpx.get(url, headers=headers)
print(f"\nResponse status: {resp.status_code}")
print(f"Response body: {resp.text[:500]}")

if resp.status_code == 200:
    data = resp.json()
    if data:
        print(f"\nWALLET FOUND: {data[0].get('wallet_address')}")
        print(f"Has encrypted key: {bool(data[0].get('encrypted_private_key'))}")
    else:
        print("\nNO ROWS RETURNED — wallet not found for this user_id")
else:
    print(f"\nERROR: {resp.status_code}")
