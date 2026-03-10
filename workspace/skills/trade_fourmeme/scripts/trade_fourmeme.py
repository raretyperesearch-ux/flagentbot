"""Four.Meme bonding curve trading — buy/sell via TokenManager2 on BSC."""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from web3 import Web3

# ── Constants ──────────────────────────────────────────────────────────────
BSC_RPC = "https://bsc-dataseed.binance.org"
CHAIN_ID = 56

FM_TM2 = Web3.to_checksum_address("0x5c952063c7fc8610FFDB798152D69F0B9550762b")
FM_HELPER = Web3.to_checksum_address("0xF251F83e40a78868FcfA3FA4599Dad6494E46034")
TREASURY = Web3.to_checksum_address("0x6c8C4C62183B61E9dd0095e821B0F857b555b32d")
ZERO_ADDR = "0x0000000000000000000000000000000000000000"
FEE_BPS = 50  # 0.5%

GAS_BUY = 300_000
GAS_SELL = 300_000
GAS_APPROVE = 100_000
GAS_FEE_TRANSFER = 21_000

# ── ABI fragments ─────────────────────────────────────────────────────────
BUY_ABI = [{
    "name": "buyTokenAMAP",
    "type": "function",
    "stateMutability": "payable",
    "inputs": [
        {"name": "token", "type": "address"},
        {"name": "funds", "type": "uint256"},
        {"name": "minAmount", "type": "uint256"},
    ],
    "outputs": [],
}]

SELL_ABI = [{
    "name": "sellToken",
    "type": "function",
    "stateMutability": "nonpayable",
    "inputs": [
        {"name": "token", "type": "address"},
        {"name": "amount", "type": "uint256"},
    ],
    "outputs": [],
}]

ERC20_ABI = [
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
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
]

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


async def _sb_insert(table: str, data: dict) -> None:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{SUPABASE_URL}/{table}", headers=_sb_headers(), json=data)
        r.raise_for_status()


# ── Wallet decryption ─────────────────────────────────────────────────────
def _get_encryption_key() -> bytes:
    raw = os.environ.get("ENCRYPTION_KEY", "")
    if not raw:
        raise RuntimeError("ENCRYPTION_KEY not set")
    if len(raw) == 64:
        return bytes.fromhex(raw)
    return raw.encode().ljust(32, b"\0")[:32]


def decrypt_private_key(encrypted_hex: str) -> str:
    data = bytes.fromhex(encrypted_hex)
    nonce, ciphertext = data[:12], data[12:]
    aesgcm = AESGCM(_get_encryption_key())
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext.decode()


# ── Core trading logic ────────────────────────────────────────────────────
async def get_user_wallet(telegram_user_id: str) -> tuple[str, str]:
    """Returns (address, private_key) from bot_users."""
    rows = await _sb_get("bot_users", {"telegram_user_id": f"eq.{telegram_user_id}", "select": "wallet_address,encrypted_key"})
    if not rows:
        raise RuntimeError("No wallet found. Run /setup first.")
    row = rows[0]
    if not row.get("encrypted_key"):
        raise RuntimeError("No encrypted key found. Run /setup first.")
    pk = decrypt_private_key(row["encrypted_key"])
    return row["wallet_address"], pk


def _build_and_send(w3: Web3, account, tx_params: dict) -> str:
    """Sign and send a transaction, return tx hash hex."""
    tx_params["nonce"] = w3.eth.get_transaction_count(account.address)
    tx_params["chainId"] = CHAIN_ID
    tx_params["gasPrice"] = w3.eth.gas_price
    signed = account.sign_transaction(tx_params)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash.hex()


async def log_position(telegram_user_id: str, token: str, side: str, bnb_amount: float, token_amount: str, tx_hash: str, platform: str) -> None:
    await _sb_insert("bot_positions", {
        "telegram_user_id": telegram_user_id,
        "token_address": token.lower(),
        "side": side,
        "bnb_amount": bnb_amount,
        "token_amount": token_amount,
        "tx_hash": tx_hash,
        "platform": platform,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


async def buy(telegram_user_id: str, token_address: str, bnb_amount: float) -> str:
    w3 = Web3(Web3.HTTPProvider(BSC_RPC))
    address, pk = await get_user_wallet(telegram_user_id)
    account = w3.eth.account.from_key(pk)
    token = Web3.to_checksum_address(token_address)

    total_wei = w3.to_wei(bnb_amount, "ether")
    fee_wei = total_wei * FEE_BPS // 10_000
    trade_wei = total_wei - fee_wei

    # 1. Send fee to treasury
    fee_hash = _build_and_send(w3, account, {
        "to": TREASURY,
        "value": fee_wei,
        "gas": GAS_FEE_TRANSFER,
    })

    # Wait for fee tx to confirm so nonce increments
    w3.eth.wait_for_transaction_receipt(fee_hash, timeout=30)

    # 2. Buy on Four.Meme
    contract = w3.eth.contract(address=FM_TM2, abi=BUY_ABI)
    tx_data = contract.functions.buyTokenAMAP(token, trade_wei, 0).build_transaction({
        "from": account.address,
        "value": trade_wei,
        "gas": GAS_BUY,
    })
    buy_hash = _build_and_send(w3, account, tx_data)

    # 3. Log position
    await log_position(telegram_user_id, token_address, "buy", bnb_amount, "0", buy_hash, "four_meme")

    return json.dumps({
        "status": "success",
        "action": "buy",
        "platform": "Four.Meme",
        "token": token_address,
        "bnb_spent": bnb_amount,
        "fee_bnb": fee_wei / 1e18,
        "trade_bnb": trade_wei / 1e18,
        "fee_tx": fee_hash,
        "buy_tx": buy_hash,
    })


async def sell(telegram_user_id: str, token_address: str, amount_or_percent: str) -> str:
    w3 = Web3(Web3.HTTPProvider(BSC_RPC))
    address, pk = await get_user_wallet(telegram_user_id)
    account = w3.eth.account.from_key(pk)
    token = Web3.to_checksum_address(token_address)

    erc20 = w3.eth.contract(address=token, abi=ERC20_ABI)
    balance = erc20.functions.balanceOf(account.address).call()

    if balance == 0:
        return json.dumps({"status": "error", "message": "No token balance to sell"})

    # Determine sell amount
    if amount_or_percent.endswith("%"):
        pct = float(amount_or_percent[:-1])
        sell_amount = int(balance * pct / 100)
    else:
        sell_amount = int(float(amount_or_percent))

    if sell_amount > balance:
        sell_amount = balance

    symbol = "?"
    try:
        symbol = erc20.functions.symbol().call()
    except Exception:
        pass

    # 1. Approve TokenManager2
    approve_tx = erc20.functions.approve(FM_TM2, sell_amount).build_transaction({
        "from": account.address,
        "gas": GAS_APPROVE,
    })
    approve_hash = _build_and_send(w3, account, approve_tx)
    w3.eth.wait_for_transaction_receipt(approve_hash, timeout=30)

    # 2. Sell on Four.Meme
    contract = w3.eth.contract(address=FM_TM2, abi=SELL_ABI)
    sell_tx = contract.functions.sellToken(token, sell_amount).build_transaction({
        "from": account.address,
        "gas": GAS_SELL,
    })
    sell_hash = _build_and_send(w3, account, sell_tx)

    # 3. Log position
    await log_position(telegram_user_id, token_address, "sell", 0, str(sell_amount), sell_hash, "four_meme")

    return json.dumps({
        "status": "success",
        "action": "sell",
        "platform": "Four.Meme",
        "token": token_address,
        "symbol": symbol,
        "amount_sold": str(sell_amount),
        "approve_tx": approve_hash,
        "sell_tx": sell_hash,
    })


async def main() -> None:
    if len(sys.argv) < 5:
        print("Usage: python trade_fourmeme.py <telegram_user_id> <buy|sell> <token_address> <amount>")
        print("  buy:  amount = BNB to spend (e.g. 0.1)")
        print("  sell: amount = token amount or percentage (e.g. 100% or 1000000)")
        sys.exit(1)

    user_id = sys.argv[1]
    action = sys.argv[2].lower()
    token = sys.argv[3]
    amount = sys.argv[4]

    if action == "buy":
        result = await buy(user_id, token, float(amount))
    elif action == "sell":
        result = await sell(user_id, token, amount)
    else:
        print(f"Unknown action: {action}. Use 'buy' or 'sell'.")
        sys.exit(1)

    print(result)


if __name__ == "__main__":
    asyncio.run(main())
