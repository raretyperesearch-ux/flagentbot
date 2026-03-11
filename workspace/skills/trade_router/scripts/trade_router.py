"""Smart trade router — auto-detects platform and routes to the correct trading script."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from web3 import Web3

# ── Constants ──────────────────────────────────────────────────────────────
BSC_RPC = "https://bsc-dataseed.binance.org"

FM_HELPER = Web3.to_checksum_address("0xF251F83e40a78868FcfA3FA4599Dad6494E46034")
FM_TM2 = Web3.to_checksum_address("0x5c952063c7fc8610FFDB798152D69F0B9550762b")
FLAP_PORTAL = Web3.to_checksum_address("0xe2cE6ab80874Fa9Fa2aAE65D277Dd6B8e65C9De0")
PANCAKE_ROUTER = Web3.to_checksum_address("0x13f4EA83D0bd40E75C8222255bc855a974568Dd4")
WBNB = Web3.to_checksum_address("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c")
ZERO_ADDR = "0x0000000000000000000000000000000000000000"

# getTokenInfo returns a 12-field struct
GET_TOKEN_INFO_ABI = [{
    "name": "getTokenInfo",
    "type": "function",
    "stateMutability": "view",
    "inputs": [{"name": "token", "type": "address"}],
    "outputs": [{
        "name": "",
        "type": "tuple",
        "components": [
            {"name": "tokenAddress", "type": "address"},
            {"name": "router", "type": "address"},
            {"name": "fundraisingGoal", "type": "uint256"},
            {"name": "currentFunds", "type": "uint256"},
            {"name": "totalSupply", "type": "uint256"},
            {"name": "remainSupply", "type": "uint256"},
            {"name": "feeRate", "type": "uint256"},
            {"name": "creator", "type": "address"},
            {"name": "pausedTrading", "type": "bool"},
            {"name": "launched", "type": "bool"},
            {"name": "liquidityAdded", "type": "bool"},
            {"name": "description", "type": "string"},
        ],
    }],
}]

# PancakeSwap V3 quoteExactInputSingle for checking if a pool exists
QUOTER_ABI = [{
    "name": "quoteExactInputSingle",
    "type": "function",
    "stateMutability": "nonpayable",
    "inputs": [{
        "name": "params",
        "type": "tuple",
        "components": [
            {"name": "tokenIn", "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "fee", "type": "uint24"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ],
    }],
    "outputs": [
        {"name": "amountOut", "type": "uint256"},
        {"name": "sqrtPriceX96After", "type": "uint160"},
        {"name": "initializedTicksCrossed", "type": "uint32"},
        {"name": "gasEstimate", "type": "uint256"},
    ],
}]

# PancakeSwap V3 Quoter V2
PANCAKE_QUOTER = Web3.to_checksum_address("0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997")

# Flap.sh quoteExactInput — used to detect if a token is on Flap.sh
FLAP_QUOTE_ABI = [{
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

SKILLS_DIR = Path(__file__).resolve().parent.parent.parent


def detect_platform(token_address: str) -> dict:
    """Detect which platform a token is on.

    Returns: {"platform": str, "details": dict}
    """
    w3 = Web3(Web3.HTTPProvider(BSC_RPC))
    token = Web3.to_checksum_address(token_address)

    # Step 1: Check Four.Meme
    try:
        helper = w3.eth.contract(address=FM_HELPER, abi=GET_TOKEN_INFO_ABI)
        info = helper.functions.getTokenInfo(token).call()
        token_addr = info[0]

        # If tokenAddress is zero, token isn't on Four.Meme
        if token_addr != "0x0000000000000000000000000000000000000000":
            liquidity_added = info[10]
            if liquidity_added:
                return {
                    "platform": "pancakeswap",
                    "reason": "Four.Meme token graduated (liquidityAdded=true)",
                    "details": {
                        "fundraisingGoal": str(info[2]),
                        "currentFunds": str(info[3]),
                        "liquidityAdded": True,
                    },
                }
            else:
                return {
                    "platform": "four_meme",
                    "reason": "Token on Four.Meme bonding curve (liquidityAdded=false)",
                    "details": {
                        "fundraisingGoal": str(info[2]),
                        "currentFunds": str(info[3]),
                        "liquidityAdded": False,
                        "pausedTrading": info[8],
                    },
                }
    except Exception:
        pass  # Not on Four.Meme, try Flap.sh

    # Step 2: Check Flap.sh — try quoting a small BNB buy
    try:
        flap = w3.eth.contract(address=FLAP_PORTAL, abi=FLAP_QUOTE_ABI)
        test_amount = w3.to_wei(0.001, "ether")
        params = (ZERO_ADDR, token, test_amount, b"")
        quote_result = flap.functions.quoteExactInput(params).call()
        if quote_result > 0:
            return {
                "platform": "flap_sh",
                "reason": "Token on Flap.sh bonding curve",
                "details": {"quote_for_0.001_bnb": str(quote_result)},
            }
    except Exception:
        pass  # Not on Flap.sh, try PancakeSwap

    # Step 3: Try PancakeSwap quote
    try:
        quoter = w3.eth.contract(address=PANCAKE_QUOTER, abi=QUOTER_ABI)
        test_amount = w3.to_wei(0.001, "ether")  # Small test amount
        params = (WBNB, token, test_amount, 2500, 0)
        result = quoter.functions.quoteExactInputSingle(params).call()
        if result[0] > 0:
            return {
                "platform": "pancakeswap",
                "reason": "Token has PancakeSwap V3 liquidity pool",
                "details": {"quote_for_0.001_bnb": str(result[0])},
            }
    except Exception:
        pass  # No PancakeSwap pool

    return {
        "platform": "unknown",
        "reason": "Token not found on any supported platform",
        "details": {},
    }


def route_trade(user_id: str, action: str, token: str, amount: str, platform: str) -> str:
    """Execute the trade via the appropriate platform script."""
    script_map = {
        "four_meme": SKILLS_DIR / "trade_fourmeme" / "scripts" / "trade_fourmeme.py",
        "pancakeswap": SKILLS_DIR / "trade_pancakeswap" / "scripts" / "trade_pancakeswap.py",
        "flap_sh": SKILLS_DIR / "trade_flapsh" / "scripts" / "trade_flapsh.py",
    }

    script = script_map.get(platform)
    if not script or not script.exists():
        return json.dumps({"status": "error", "message": f"No trading script for platform: {platform}"})

    try:
        result = subprocess.run(
            [sys.executable, str(script), user_id, action, token, amount],
            capture_output=True, text=True, timeout=60,
            env={**os.environ},
        )
        if result.returncode != 0:
            return json.dumps({
                "status": "error",
                "message": result.stderr[:500] if result.stderr else "Trade script failed",
            })
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return json.dumps({"status": "error", "message": "Trade timed out after 60s"})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


async def main() -> None:
    if len(sys.argv) < 5:
        print("Usage: python trade_router.py <telegram_user_id> <buy|sell> <token_address> <amount>")
        print("\nAuto-detects platform (Four.Meme, PancakeSwap, Flap.sh) and routes the trade.")
        sys.exit(1)

    user_id = sys.argv[1]
    action = sys.argv[2].lower()
    token = sys.argv[3]
    amount = sys.argv[4]

    if action not in ("buy", "sell"):
        print(f"Unknown action: {action}. Use 'buy' or 'sell'.")
        sys.exit(1)

    # Detect platform
    detection = detect_platform(token)
    platform = detection["platform"]

    if platform == "unknown":
        print(json.dumps({
            "status": "error",
            "message": "Token not found on any supported platform (Four.Meme, Flap.sh, PancakeSwap).",
            "detection": detection,
        }))
        sys.exit(1)

    print(f"Detected: {detection['reason']}", file=sys.stderr)
    print(f"Routing to: {platform}", file=sys.stderr)

    # Execute trade
    result = route_trade(user_id, action, token, amount, platform)
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
