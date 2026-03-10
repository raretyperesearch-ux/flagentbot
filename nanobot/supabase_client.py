"""Shared Supabase HTTP client for FlagentBot multi-tenant storage."""

from __future__ import annotations

import os
from typing import Any

import httpx
from loguru import logger

_SUPABASE_URL = "https://seartddspffufwiqzwvh.supabase.co"
_REST_SUFFIX = "/rest/v1"


def _headers() -> dict[str, str]:
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not key:
        logger.warning("SUPABASE_SERVICE_KEY not set — database calls will fail")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _url(table: str) -> str:
    return f"{_SUPABASE_URL}{_REST_SUFFIX}/{table}"


async def select(
    table: str,
    params: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """SELECT rows from a Supabase table. Returns list of row dicts."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(_url(table), headers=_headers(), params=params or {})
        resp.raise_for_status()
        return resp.json()


async def upsert(
    table: str,
    data: dict[str, Any] | list[dict[str, Any]],
    on_conflict: str = "",
) -> list[dict[str, Any]]:
    """UPSERT (insert or update) rows. Returns the upserted rows."""
    headers = _headers()
    if on_conflict:
        headers["Prefer"] = "return=representation,resolution=merge-duplicates"
    else:
        headers["Prefer"] = "return=representation"
    params = {}
    if on_conflict:
        params["on_conflict"] = on_conflict
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _url(table), headers=headers, json=data if isinstance(data, list) else [data],
            params=params,
        )
        resp.raise_for_status()
        return resp.json()


async def insert(
    table: str,
    data: dict[str, Any] | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """INSERT rows. Returns inserted rows."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _url(table), headers=_headers(),
            json=data if isinstance(data, list) else [data],
        )
        resp.raise_for_status()
        return resp.json()


async def update(
    table: str,
    data: dict[str, Any],
    match: dict[str, str],
) -> list[dict[str, Any]]:
    """UPDATE rows matching filters. Returns updated rows."""
    params = {f"{k}": f"eq.{v}" for k, v in match.items()}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.patch(
            _url(table), headers=_headers(), json=data, params=params,
        )
        resp.raise_for_status()
        return resp.json()
