"""Flap.sh Portal trading — buy/sell on BSC."""

import asyncio
import base64
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

FLAP_PORTAL = Web3.to_checksum_address("0xe2cE6ab80874Fa9Fa2aAE65D277Dd6B8e65C9De0")
TREASURY = Web3.to_checksum_address("0x6c8C4C62183B61E9dd0095e821B0F857b555b32d")
ZERO_ADDR = "0x0000000000000000000000000000000000000000"
FEE_BPS = 50  # 0.5%

GAS_BUY = 350_000
GAS_SELL = 350_000
GAS_APPROVE = 100_000
GAS_FEE_TRANSFER = 21_000

# ── ABI fragments ─────────────────────────────────────────────────────────
BUY_ABI = [{
    "name": "buy",
    "type": "function",
    "stateMutability": "payable",
    "inputs": [
        {"name": "token", "type": "address"},
        {"name": "recipient", "type": "address"},
        {"name": "minAmount", "type": "uint256"},
    ],
    "outputs": [{"name": "amount", "type": "uint256"}],
}]

SELL_ABI = [{
    "name": "swapExactInput",
    "type": "function",
    "stateMutability": "payable",
    "inputs": [
        {
            "name": "params",
            "type": "tuple",
            "components": [
                {"name": "inputToken", "type": "address"},
                {"name": "outputToken", "type": "address"},
                {"name": "inputAmount", "type": "uint256"},
                {"name": "minOutputAmount", "type": "uint256"},
                {"name": "permitData", "type": "bytes"},
            ],
        }
    ],
    "outputs": [],
}]

QUOTE_ABI = [{
    "name": "quoteExactInput",
    "type": "function",
    "stateMutability": "view",
    "inputs": [{
        "name": "params",
        "type": "tuple",
        "components": [
            {"name": "inputToken", "type": "address"},
            {"name": "outputToken", "type": "address"},
            {"name": "inputAmount", "type": "uint256"},
            {"name": "permitData", "type": "bytes"},
        ],
    }],
    "outputs": [{"name": "outputAmount", "type": "uint256"}],
}]

SLIPPAGE_BPS = 500  # 5% slippage tolerance

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
    raise RuntimeError("ENCRYPTION_KEY must be exactly 64 hex characters (32 bytes)")


def decrypt_private_key(encrypted_b64: str) -> str:
    """Decrypt a base64-encoded AES-256-GCM encrypted private key (matches loop.py)."""
    data = base64.b64decode(encrypted_b64)
    nonce, ciphertext = data[:12], data[12:]
    aesgcm = AESGCM(_get_encryption_key())
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext.decode()


async def get_user_wallet(telegram_user_id: str) -> tuple[str, str]:
    rows = await _sb_get("bot_users", {"telegram_user_id": f"eq.{telegram_user_id}", "select": "wallet_address,encrypted_private_key"})
    if not rows:
        raise RuntimeError("No wallet found. Run /setup first.")
    row = rows[0]
    if not row.get("encrypted_private_key"):
        raise RuntimeError("No encrypted key found. Run /setup first.")
    pk = decrypt_private_key(row["encrypted_private_key"])
    return row["wallet_address"], pk


# ── Helpers ────────────────────────────────────────────────────────────────
MAX_GAS_PRICE = Web3.to_wei(10, "gwei")


def _build_and_send(w3: Web3, account, tx_params: dict) -> str:
    tx_params["nonce"] = w3.eth.get_transaction_count(account.address)
    tx_params["chainId"] = CHAIN_ID
    tx_params["gasPrice"] = min(w3.eth.gas_price, MAX_GAS_PRICE)
    # Estimate gas with 20% buffer, fall back to hardcoded value
    if "gas" in tx_params:
        fallback_gas = tx_params["gas"]
        try:
            estimated = w3.eth.estimate_gas({k: v for k, v in tx_params.items() if k != "gas"})
            tx_params["gas"] = int(estimated * 1.2)
        except Exception:
            tx_params["gas"] = fallback_gas
    signed = account.sign_transaction(tx_params)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash.hex()


async def log_position(telegram_user_id: str, token: str, side: str, cost_bnb: float, amount_tokens: str, tx_hash: str) -> None:
    row = {
        "user_id": telegram_user_id,
        "token_address": token.lower(),
        "side": side,
        "cost_bnb": cost_bnb,
        "amount_tokens": amount_tokens,
        "platform": "flap_sh",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if side == "buy":
        row["tx_hash_buy"] = tx_hash
    else:
        row["tx_hash_sell"] = tx_hash
    await _sb_insert("bot_positions", row)


# ── Error helper ───────────────────────────────────────────────────────────
def _error(what: str, why: str, fix: str) -> str:
    return json.dumps({"status": "error", "message": f"\u274c {what}\n{why}\n{fix}"})


# ── Trading ────────────────────────────────────────────────────────────────
async def buy(telegram_user_id: str, token_address: str, bnb_amount: float) -> str:
    try:
        w3 = Web3(Web3.HTTPProvider(BSC_RPC))
    except Exception:
        return _error("RPC connection failed", "Could not connect to BSC network.", "Try again in a moment.")

    try:
        address, pk = await get_user_wallet(telegram_user_id)
    except RuntimeError as e:
        return _error("No wallet found", str(e), "Run /setup to create your wallet.")

    account = w3.eth.account.from_key(pk)
    token = Web3.to_checksum_address(token_address)

    total_wei = w3.to_wei(bnb_amount, "ether")
    fee_wei = total_wei * FEE_BPS // 10_000
    trade_wei = total_wei - fee_wei

    # Check BNB balance
    balance = w3.eth.get_balance(account.address)
    needed = total_wei + w3.to_wei(0.002, "ether")
    if balance < needed:
        have = w3.from_wei(balance, "ether")
        return _error(
            "Insufficient BNB",
            f"You have {have:.4f} BNB but need ~{w3.from_wei(needed, 'ether'):.4f} BNB (trade + gas).",
            "Deposit more BNB to your wallet. Run /deposit to see your address.",
        )

    # 1. Send fee to treasury
    try:
        fee_hash = _build_and_send(w3, account, {
            "to": TREASURY,
            "value": fee_wei,
            "gas": GAS_FEE_TRANSFER,
        })
        w3.eth.wait_for_transaction_receipt(fee_hash, timeout=30)
    except Exception as e:
        return _error("Fee transfer failed", str(e)[:200], "Try again in a moment.")

    # 2. Get quote for slippage protection
    min_tokens = 0
    try:
        quoter = w3.eth.contract(address=FLAP_PORTAL, abi=QUOTE_ABI)
        params = (ZERO_ADDR, token, trade_wei, b"")
        estimated = quoter.functions.quoteExactInput(params).call()
        min_tokens = estimated * (10_000 - SLIPPAGE_BPS) // 10_000
    except Exception:
        pass

    # 3. Buy on Flap.sh
    try:
        contract = w3.eth.contract(address=FLAP_PORTAL, abi=BUY_ABI)
        tx_data = contract.functions.buy(token, account.address, min_tokens).build_transaction({
            "from": account.address,
            "value": trade_wei,
            "gas": GAS_BUY,
        })
        buy_hash = _build_and_send(w3, account, tx_data)
    except Exception as e:
        err_str = str(e)[:200]
        if "revert" in err_str.lower() or "execution reverted" in err_str.lower():
            return _error("Transaction reverted", "The Flap.sh contract rejected this trade.", "The token may not be tradeable right now. Try a smaller amount.")
        return _error("Buy failed", err_str, "Try again or use a smaller amount.")

    await log_position(telegram_user_id, token_address, "buy", bnb_amount, "0", buy_hash)

    return json.dumps({
        "status": "success",
        "action": "buy",
        "platform": "Flap.sh",
        "token": token_address,
        "bnb_spent": bnb_amount,
        "fee_bnb": fee_wei / 1e18,
        "trade_bnb": trade_wei / 1e18,
        "fee_tx": fee_hash,
        "buy_tx": buy_hash,
    })


async def sell(telegram_user_id: str, token_address: str, amount_or_percent: str) -> str:
    try:
        w3 = Web3(Web3.HTTPProvider(BSC_RPC))
    except Exception:
        return _error("RPC connection failed", "Could not connect to BSC network.", "Try again in a moment.")

    try:
        address, pk = await get_user_wallet(telegram_user_id)
    except RuntimeError as e:
        return _error("No wallet found", str(e), "Run /setup to create your wallet.")

    account = w3.eth.account.from_key(pk)
    token = Web3.to_checksum_address(token_address)

    erc20 = w3.eth.contract(address=token, abi=ERC20_ABI)
    balance = erc20.functions.balanceOf(account.address).call()

    if balance == 0:
        return _error("No tokens to sell", "Your wallet holds 0 of this token.", "Make sure you're using the correct token address.")

    symbol = "?"
    try:
        symbol = erc20.functions.symbol().call()
    except Exception:
        pass

    # Determine sell amount from on-chain balance
    if amount_or_percent.endswith("%"):
        pct = float(amount_or_percent[:-1])
        sell_amount = int(balance * pct / 100)
    else:
        sell_amount = int(float(amount_or_percent))
        if sell_amount > balance:
            return _error(
                "Insufficient token balance",
                f"You hold {balance} {symbol} but tried to sell {sell_amount}.",
                "Use 'sell 100%' to sell your full balance.",
            )

    # Check gas
    gas_balance = w3.eth.get_balance(account.address)
    if gas_balance < w3.to_wei(0.002, "ether"):
        return _error("Insufficient BNB for gas", f"You have {w3.from_wei(gas_balance, 'ether'):.4f} BNB.", "Deposit at least 0.002 BNB for gas fees.")

    # 1. Get sell quote for slippage protection
    min_output = 0
    try:
        quoter = w3.eth.contract(address=FLAP_PORTAL, abi=QUOTE_ABI)
        params_q = (token, ZERO_ADDR, sell_amount, b"")
        estimated = quoter.functions.quoteExactInput(params_q).call()
        min_output = estimated * (10_000 - SLIPPAGE_BPS) // 10_000
    except Exception:
        pass

    # 2. Approve Flap.sh Portal
    try:
        approve_tx = erc20.functions.approve(FLAP_PORTAL, sell_amount).build_transaction({
            "from": account.address,
            "gas": GAS_APPROVE,
        })
        approve_hash = _build_and_send(w3, account, approve_tx)
        w3.eth.wait_for_transaction_receipt(approve_hash, timeout=30)
    except Exception as e:
        return _error("Approve failed", str(e)[:200], "Try again in a moment.")

    # 3. Sell via swapExactInput
    try:
        contract = w3.eth.contract(address=FLAP_PORTAL, abi=SELL_ABI)
        params = (token, ZERO_ADDR, sell_amount, min_output, b"")
        sell_tx = contract.functions.swapExactInput(params).build_transaction({
            "from": account.address,
            "gas": GAS_SELL,
        })
        sell_hash = _build_and_send(w3, account, sell_tx)
    except Exception as e:
        err_str = str(e)[:200]
        if "revert" in err_str.lower() or "execution reverted" in err_str.lower():
            return _error("Transaction reverted", "The Flap.sh contract rejected this sell.", "The token may not be tradeable right now. Try again later or try a smaller amount.")
        return _error("Sell failed", err_str, "Try again or use a smaller amount.")

    await log_position(telegram_user_id, token_address, "sell", 0, str(sell_amount), sell_hash)

    return json.dumps({
        "status": "success",
        "action": "sell",
        "platform": "Flap.sh",
        "token": token_address,
        "symbol": symbol,
        "amount_sold": str(sell_amount),
        "approve_tx": approve_hash,
        "sell_tx": sell_hash,
    })


async def main() -> None:
    if len(sys.argv) < 5:
        print("Usage: python trade_flapsh.py <telegram_user_id> <buy|sell> <token_address> <amount>")
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
