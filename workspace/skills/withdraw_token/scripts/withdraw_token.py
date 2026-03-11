"""Token withdrawal — transfer ERC-20 tokens to an external BSC address."""

import asyncio
import base64
import json
import os
import sys

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from web3 import Web3

# ── Constants ──────────────────────────────────────────────────────────────
BSC_RPC = "https://bsc-dataseed.binance.org"
CHAIN_ID = 56
GAS_TRANSFER = 100_000
MAX_GAS_PRICE = Web3.to_wei(10, "gwei")
SUPABASE_URL = "https://seartddspffufwiqzwvh.supabase.co/rest/v1"

ERC20_ABI = [
    {
        "name": "transfer",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "symbol",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
    },
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
]


# ── Error helper ───────────────────────────────────────────────────────────
def _error(what: str, why: str, fix: str) -> str:
    return json.dumps({"status": "error", "message": f"\u274c {what}\n{why}\n{fix}"})


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


# ── Wallet decryption ─────────────────────────────────────────────────────
def _get_encryption_key() -> bytes:
    raw = os.environ.get("ENCRYPTION_KEY", "")
    if not raw:
        raise RuntimeError("ENCRYPTION_KEY not set")
    if len(raw) == 64:
        return bytes.fromhex(raw)
    raise RuntimeError("ENCRYPTION_KEY must be exactly 64 hex characters (32 bytes)")


def decrypt_private_key(encrypted_b64: str) -> str:
    """Decrypt a base64-encoded AES-256-GCM encrypted private key (matches loop.py)."""
    data = base64.b64decode(encrypted_b64)
    nonce, ciphertext = data[:12], data[12:]
    aesgcm = AESGCM(_get_encryption_key())
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext.decode()


async def get_user_wallet(telegram_user_id: str) -> tuple[str, str]:
    rows = await _sb_get("bot_users", {
        "telegram_user_id": f"eq.{telegram_user_id}",
        "select": "wallet_address,encrypted_private_key",
    })
    if not rows:
        raise RuntimeError("No wallet found. Run /setup first.")
    row = rows[0]
    if not row.get("encrypted_private_key"):
        raise RuntimeError("No encrypted key found. Run /setup first.")
    pk = decrypt_private_key(row["encrypted_private_key"])
    return row["wallet_address"], pk


# ── Core withdrawal ───────────────────────────────────────────────────────
async def withdraw_token(
    telegram_user_id: str,
    token_address: str,
    destination: str,
    amount_str: str,
) -> str:
    try:
        w3 = Web3(Web3.HTTPProvider(BSC_RPC))
        from web3.middleware import ExtraDataToPOAMiddleware
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    except Exception:
        return _error("RPC connection failed", "Could not connect to BSC network.", "Try again in a moment.")

    try:
        address, pk = await get_user_wallet(telegram_user_id)
    except RuntimeError as e:
        return _error("No wallet found", str(e), "Run /setup to create your wallet.")

    account = w3.eth.account.from_key(pk)
    token = Web3.to_checksum_address(token_address)
    dest = Web3.to_checksum_address(destination)

    erc20 = w3.eth.contract(address=token, abi=ERC20_ABI)

    # Get symbol and decimals
    symbol = "?"
    decimals = 18
    try:
        symbol = erc20.functions.symbol().call()
    except Exception:
        pass
    try:
        decimals = erc20.functions.decimals().call()
    except Exception:
        pass

    # Get balance
    balance = erc20.functions.balanceOf(account.address).call()
    if balance == 0:
        return _error(
            "No tokens to withdraw",
            f"Your wallet holds 0 {symbol}.",
            "Make sure you're using the correct token address.",
        )

    # Determine amount — treat user input as human-readable (multiply by 10**decimals)
    if amount_str.lower() in ("all", "100%"):
        send_amount = balance
    else:
        try:
            human_amount = float(amount_str)
        except ValueError:
            return _error("Invalid amount", f"'{amount_str}' is not a valid number.", "Use a number or 'all'.")
        if human_amount <= 0:
            return _error("Invalid amount", "Amount must be greater than 0.", "Use a positive number or 'all'.")
        send_amount = int(human_amount * (10 ** decimals))
        if send_amount > balance:
            human_balance = balance / (10 ** decimals)
            return _error(
                "Insufficient balance",
                f"You hold {human_balance:,.{min(decimals, 6)}f} {symbol} but tried to send {human_amount:,.{min(decimals, 6)}f}.",
                "Use 'all' to send your full balance.",
            )

    # Check gas
    gas_balance = w3.eth.get_balance(account.address)
    if gas_balance < w3.to_wei(0.001, "ether"):
        return _error(
            "Need BNB for gas",
            f"Your BNB balance: {w3.from_wei(gas_balance, 'ether'):.4f}.",
            "Send at least 0.001 BNB to your wallet.",
        )

    # Build and send ERC20 transfer
    try:
        tx_data = erc20.functions.transfer(dest, send_amount).build_transaction({
            "from": account.address,
            "gas": GAS_TRANSFER,
        })
        tx_data["nonce"] = w3.eth.get_transaction_count(account.address)
        tx_data["chainId"] = CHAIN_ID
        tx_data["gasPrice"] = min(w3.eth.gas_price, MAX_GAS_PRICE)
        # Estimate gas with 20% buffer, fall back to hardcoded
        try:
            estimated = w3.eth.estimate_gas({k: v for k, v in tx_data.items() if k != "gas"})
            tx_data["gas"] = int(estimated * 1.2)
        except Exception:
            pass  # keep GAS_TRANSFER fallback
        signed = account.sign_transaction(tx_data)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
    except Exception as e:
        err_str = str(e)[:200]
        if "revert" in err_str.lower() or "execution reverted" in err_str.lower():
            return _error("Transfer reverted", "The token contract rejected this transfer.", "The token may restrict transfers. Check if it has transfer limits or is paused.")
        return _error("Transfer failed", err_str, "Try again in a moment.")

    human_sent = send_amount / (10 ** decimals)
    return json.dumps({
        "status": "success",
        "tx_hash": tx_hash,
        "token": token_address,
        "symbol": symbol,
        "amount": str(send_amount),
        "human_amount": f"{human_sent:,.{min(decimals, 6)}f}",
        "destination": destination,
    })


async def main() -> None:
    if len(sys.argv) < 5:
        print("Usage: python withdraw_token.py <telegram_user_id> <token_address> <destination> <amount|all>")
        sys.exit(1)

    result = await withdraw_token(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
