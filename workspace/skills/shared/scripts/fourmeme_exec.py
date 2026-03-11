#!/usr/bin/env python3
"""Decrypt user wallet key, write temp .env, run fourmeme CLI command."""
import os, sys, base64, subprocess, tempfile
import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def get_user_key(telegram_user_id: str):
    """Decrypt user's private key from Supabase."""
    sb_url = "https://seartddspffufwiqzwvh.supabase.co"
    sb_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not sb_key:
        print("ERROR: SUPABASE_SERVICE_KEY not set")
        sys.exit(1)

    resp = httpx.get(
        f"{sb_url}/rest/v1/bot_users?telegram_user_id=eq.{telegram_user_id}&select=wallet_address,encrypted_private_key",
        headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"}
    )
    data = resp.json()
    if not data:
        print(f"ERROR: No wallet found for user {telegram_user_id}")
        sys.exit(1)

    enc = data[0]["encrypted_private_key"]
    raw = base64.b64decode(enc)
    nonce, ct = raw[:12], raw[12:]
    key_hex = os.environ.get("ENCRYPTION_KEY", "")
    if not key_hex:
        print("ERROR: ENCRYPTION_KEY not set")
        sys.exit(1)
    aes_key = bytes.fromhex(key_hex)
    private_key = AESGCM(aes_key).decrypt(nonce, ct, None).decode()
    wallet = data[0]["wallet_address"]
    return private_key, wallet


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: fourmeme_exec.py <telegram_user_id> <fourmeme args...>")
        print("Example: fourmeme_exec.py 6850147865 quote-buy 0xABC 0 50000000000000000")
        sys.exit(1)

    user_id = sys.argv[1]
    fm_args = sys.argv[2:]

    private_key, wallet = get_user_key(user_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        env_path = os.path.join(tmpdir, ".env")
        with open(env_path, "w") as f:
            f.write(f"PRIVATE_KEY={private_key}\n")
            f.write(f"BSC_RPC_URL=https://bsc-dataseed.binance.org\n")

        print(f"Wallet: {wallet}", file=sys.stderr)
        print(f"Running: fourmeme {' '.join(fm_args)}", file=sys.stderr)

        result = subprocess.run(
            ["fourmeme"] + list(fm_args),
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=60
        )
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        sys.exit(result.returncode)
