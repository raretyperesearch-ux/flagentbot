"""Per-user memory system backed by Supabase (bot_user_memory table)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from loguru import logger

from nanobot import supabase_client as db

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import Session


_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph (2-5 sentences) summarizing key events/decisions/topics. "
                        "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search.",
                    },
                    "memory_update": {
                        "type": "string",
                        "description": "Full updated long-term memory as markdown. Include all existing "
                        "facts plus new ones. Return unchanged if nothing new.",
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]


class MemoryStore:
    """Per-user two-layer memory backed by Supabase bot_user_memory table.

    Each Telegram user gets their own memory_text (long-term facts) and
    history_entries (timestamped log), keyed by telegram_user_id.
    """

    def __init__(self, telegram_user_id: str | None = None):
        self.telegram_user_id = telegram_user_id

    # -- read / write helpers --------------------------------------------------

    async def _get_row(self) -> dict | None:
        """Fetch the memory row for the current user."""
        if not self.telegram_user_id:
            return None
        rows = await db.select(
            "bot_user_memory",
            {"telegram_user_id": f"eq.{self.telegram_user_id}", "select": "*"},
        )
        return rows[0] if rows else None

    async def _ensure_row(self) -> None:
        """Create the memory row if it doesn't exist yet."""
        if not self.telegram_user_id:
            return
        await db.upsert(
            "bot_user_memory",
            {"telegram_user_id": self.telegram_user_id, "memory_text": "", "history_entries": ""},
            on_conflict="telegram_user_id",
        )

    async def read_long_term(self) -> str:
        row = await self._get_row()
        return (row or {}).get("memory_text", "") or ""

    async def write_long_term(self, content: str) -> None:
        if not self.telegram_user_id:
            return
        await self._ensure_row()
        await db.update(
            "bot_user_memory",
            {"memory_text": content},
            {"telegram_user_id": self.telegram_user_id},
        )

    async def append_history(self, entry: str) -> None:
        if not self.telegram_user_id:
            return
        await self._ensure_row()
        row = await self._get_row()
        existing = (row or {}).get("history_entries", "") or ""
        updated = existing + entry.rstrip() + "\n\n"
        await db.update(
            "bot_user_memory",
            {"history_entries": updated},
            {"telegram_user_id": self.telegram_user_id},
        )

    async def get_memory_context(self) -> str:
        long_term = await self.read_long_term()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    # -- LLM-driven consolidation ----------------------------------------------

    async def consolidate(
        self,
        session: Session,
        provider: LLMProvider,
        model: str,
        *,
        archive_all: bool = False,
        memory_window: int = 50,
    ) -> bool:
        """Consolidate old messages into Supabase memory via LLM tool call.

        Returns True on success (including no-op), False on failure.
        """
        if archive_all:
            old_messages = session.messages
            keep_count = 0
            logger.info("Memory consolidation (archive_all): {} messages", len(session.messages))
        else:
            keep_count = memory_window // 2
            if len(session.messages) <= keep_count:
                return True
            if len(session.messages) - session.last_consolidated <= 0:
                return True
            old_messages = session.messages[session.last_consolidated:-keep_count]
            if not old_messages:
                return True
            logger.info("Memory consolidation: {} to consolidate, {} keep", len(old_messages), keep_count)

        lines = []
        for m in old_messages:
            if not m.get("content"):
                continue
            tools = f" [tools: {', '.join(m['tools_used'])}]" if m.get("tools_used") else ""
            lines.append(f"[{m.get('timestamp', '?')[:16]}] {m['role'].upper()}{tools}: {m['content']}")

        current_memory = await self.read_long_term()
        prompt = f"""Process this conversation and call the save_memory tool with your consolidation.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{chr(10).join(lines)}"""

        try:
            response = await provider.chat_with_retry(
                messages=[
                    {"role": "system", "content": "You are a memory consolidation agent. Call the save_memory tool with your consolidation of the conversation."},
                    {"role": "user", "content": prompt},
                ],
                tools=_SAVE_MEMORY_TOOL,
                model=model,
            )

            if not response.has_tool_calls:
                logger.warning("Memory consolidation: LLM did not call save_memory, skipping")
                return False

            args = response.tool_calls[0].arguments
            if isinstance(args, str):
                args = json.loads(args)
            if isinstance(args, list):
                if args and isinstance(args[0], dict):
                    args = args[0]
                else:
                    logger.warning("Memory consolidation: unexpected arguments as empty or non-dict list")
                    return False
            if not isinstance(args, dict):
                logger.warning("Memory consolidation: unexpected arguments type {}", type(args).__name__)
                return False

            if entry := args.get("history_entry"):
                if not isinstance(entry, str):
                    entry = json.dumps(entry, ensure_ascii=False)
                await self.append_history(entry)
            if update := args.get("memory_update"):
                if not isinstance(update, str):
                    update = json.dumps(update, ensure_ascii=False)
                if update != current_memory:
                    await self.write_long_term(update)

            session.last_consolidated = 0 if archive_all else len(session.messages) - keep_count
            logger.info("Memory consolidation done: {} messages, last_consolidated={}", len(session.messages), session.last_consolidated)
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return False
