"""Shared price engine — resolves token price from multiple BSC sources."""

import asyncio

import httpx
from web3 import Web3

# ── Constants ──────────────────────────────────────────────────────────────
BSC_RPC = "https://bsc-dataseed.binance.org"
WBNB = Web3.to_checksum_address("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c")
ZERO_ADDR = "0x0000000000000000000000000000000000000000"

FM_HELPER = Web3.to_checksum_address("0xF251F83e40a78868FcfA3FA4599Dad6494E46034")
FLAP_PORTAL = Web3.to_checksum_address("0xe2cE6ab80874Fa9Fa2aAE65D277Dd6B8e65C9De0")
PANCAKE_QUOTER = Web3.to_checksum_address("0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997")

# ABIs
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

TRY_BUY_ABI = [{
    "name": "tryBuy",
    "type": "function",
    "stateMutability": "view",
    "inputs": [
        {"name": "token", "type": "address"},
        {"name": "funds", "type": "uint256"},
    ],
    "outputs": [{"name": "amount", "type": "uint256"}],
}]

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


# ── BNB price cache (5 minutes) ───────────────────────────────────────────
_bnb_price_cache: dict[str, float] = {}  # {"price": x, "ts": y}


async def get_bnb_price() -> float | None:
    """Public alias for getting BNB/USD price."""
    return await _get_bnb_price_usd()


async def _get_bnb_price_usd() -> float | None:
    """Fetch BNB/USD price from DexScreener. Cached for 5 minutes.
    Returns None if price cannot be determined.
    """
    import time as _time
    cached_ts = _bnb_price_cache.get("ts", 0)
    if _time.time() - cached_ts < 300 and "price" in _bnb_price_cache:
        return _bnb_price_cache["price"]

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://api.dexscreener.com/latest/dex/tokens/{WBNB}")
            r.raise_for_status()
            pairs = r.json().get("pairs", [])
            for p in pairs:
                if p.get("chainId") == "bsc" and p.get("priceUsd"):
                    price = float(p["priceUsd"])
                    _bnb_price_cache["price"] = price
                    _bnb_price_cache["ts"] = _time.time()
                    return price
    except Exception:
        pass
    # Return cached value if available, even if expired
    if "price" in _bnb_price_cache:
        return _bnb_price_cache["price"]
    return None


async def _try_fourmeme(token: str, w3: Web3) -> dict | None:
    """Check Four.Meme — returns price info if token is on bonding curve."""
    try:
        helper = w3.eth.contract(address=FM_HELPER, abi=GET_TOKEN_INFO_ABI)
        info = helper.functions.getTokenInfo(Web3.to_checksum_address(token)).call()
        token_addr = info[0]
        if token_addr == ZERO_ADDR:
            return None

        liquidity_added = info[10]
        if liquidity_added:
            return None  # graduated — use DEX price instead

        # On bonding curve: estimate price via tryBuy with 1 BNB
        try:
            helper2 = w3.eth.contract(address=FM_HELPER, abi=TRY_BUY_ABI)
            one_bnb = w3.to_wei(1, "ether")
            tokens_for_1bnb = helper2.functions.tryBuy(
                Web3.to_checksum_address(token), one_bnb
            ).call()
            if tokens_for_1bnb > 0:
                price_bnb = 1e18 / tokens_for_1bnb  # price per token in BNB (18 dec)
                return {
                    "price_bnb": price_bnb,
                    "source": "four_meme",
                    "graduated": False,
                    "liquidity_usd": None,
                    "volume_24h_usd": None,
                }
        except Exception:
            pass
        return None
    except Exception:
        return None


async def _try_dexscreener(token: str) -> dict | None:
    """Fetch price from DexScreener API."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://api.dexscreener.com/latest/dex/tokens/{token}")
            r.raise_for_status()
            pairs = r.json().get("pairs", [])
            # Find best BSC pair by liquidity
            bsc_pairs = [p for p in pairs if p.get("chainId") == "bsc"]
            if not bsc_pairs:
                return None
            best = max(bsc_pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
            price_usd = float(best.get("priceUsd", 0) or 0)
            if price_usd <= 0:
                return None
            bnb_price = await _get_bnb_price_usd()
            return {
                "price_bnb": price_usd / bnb_price if bnb_price and bnb_price > 0 else 0,
                "price_usd": price_usd,
                "source": "dexscreener",
                "graduated": True,
                "liquidity_usd": float(best.get("liquidity", {}).get("usd", 0) or 0),
                "volume_24h_usd": float(best.get("volume", {}).get("h24", 0) or 0),
            }
    except Exception:
        return None


async def _try_pancakeswap(token: str, w3: Web3) -> dict | None:
    """Get price from PancakeSwap V3 quoter."""
    try:
        quoter = w3.eth.contract(address=PANCAKE_QUOTER, abi=QUOTER_ABI)
        one_bnb = w3.to_wei(1, "ether")
        params = (WBNB, Web3.to_checksum_address(token), one_bnb, 2500, 0)
        result = quoter.functions.quoteExactInputSingle(params).call()
        tokens_out = result[0]
        if tokens_out > 0:
            price_bnb = 1e18 / tokens_out
            return {
                "price_bnb": price_bnb,
                "source": "pancakeswap",
                "graduated": True,
                "liquidity_usd": None,
                "volume_24h_usd": None,
            }
    except Exception:
        pass
    return None


async def get_token_price(token_address: str) -> dict:
    """Get token price from best available source.

    Returns: {"price_bnb", "price_usd", "source", "liquidity_usd", "volume_24h_usd", "graduated"}
    """
    w3 = Web3(Web3.HTTPProvider(BSC_RPC))
    from web3.middleware import ExtraDataToPOAMiddleware
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    # 1. Four.Meme (bonding curve tokens)
    fm = await _try_fourmeme(token_address, w3)
    if fm:
        bnb_usd = await _get_bnb_price_usd()
        fm["price_usd"] = fm["price_bnb"] * bnb_usd if bnb_usd else None
        return fm

    # 2. DexScreener (graduated tokens — best data)
    ds = await _try_dexscreener(token_address)
    if ds:
        return ds

    # 3. PancakeSwap quoter (fallback)
    pcs = await _try_pancakeswap(token_address, w3)
    if pcs:
        bnb_usd = await _get_bnb_price_usd()
        pcs["price_usd"] = pcs["price_bnb"] * bnb_usd if bnb_usd else None
        return pcs

    return {
        "price_bnb": 0,
        "price_usd": 0,
        "source": "unknown",
        "graduated": None,
        "liquidity_usd": None,
        "volume_24h_usd": None,
    }
