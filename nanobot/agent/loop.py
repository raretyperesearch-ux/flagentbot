"""Agent loop: the core processing engine for FlagentBot."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
import weakref
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import Session, SessionManager
from nanobot import supabase_client as db

if TYPE_CHECKING:
    from nanobot.config.schema import ChannelsConfig, ExecToolConfig
    from nanobot.cron.service import CronService


# -- helpers for /setup wallet generation ------------------------------------

def _parse_telegram_user_id(sender_id: str) -> str:
    """Extract numeric Telegram user ID from sender_id (format: 'id|username' or just 'id')."""
    return sender_id.split("|", 1)[0]


def _encrypt_private_key(private_key_hex: str) -> str:
    """Encrypt a private key with AES-256-GCM using ENCRYPTION_KEY env var.
    Returns base64-encoded (nonce + ciphertext + tag).
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    enc_key = os.environ.get("ENCRYPTION_KEY", "")
    if not enc_key:
        raise RuntimeError("ENCRYPTION_KEY env var not set — cannot encrypt wallet key")
    # Key should be 32 bytes; derive from hex or utf-8
    if len(enc_key) == 64:
        key_bytes = bytes.fromhex(enc_key)
    else:
        key_bytes = enc_key.encode("utf-8")[:32].ljust(32, b"\0")
    aesgcm = AESGCM(key_bytes)
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, private_key_hex.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("ascii")


def _decrypt_private_key(encrypted_b64: str) -> str:
    """Decrypt a private key previously encrypted with _encrypt_private_key."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    enc_key = os.environ.get("ENCRYPTION_KEY", "")
    if not enc_key:
        raise RuntimeError("ENCRYPTION_KEY env var not set")
    if len(enc_key) == 64:
        key_bytes = bytes.fromhex(enc_key)
    else:
        key_bytes = enc_key.encode("utf-8")[:32].ljust(32, b"\0")
    data = base64.b64decode(encrypted_b64)
    nonce, ct = data[:12], data[12:]
    aesgcm = AESGCM(key_bytes)
    return aesgcm.decrypt(nonce, ct, None).decode("utf-8")


# -- hold-to-use threshold ---------------------------------------------------

_MIN_FLAGENT_HOLD = 25_000  # Must hold 25k $FLAGENT for full access
_FLAGENT_CA = "0x1FF3506b0BC80c3CA027B6cEb7534FcfeDccFFFF"
_PCS_SWAP_URL = f"https://pancakeswap.finance/swap?outputCurrency={_FLAGENT_CA}"


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Checks user balance ($FLAGENT)
    3. Builds context with history, per-user memory, skills
    4. Calls the LLM
    5. Executes tool calls
    6. Sends responses back
    7. Deducts $FLAGENT and logs usage
    """

    _TOOL_RESULT_MAX_CHARS = 500

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 40,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        memory_window: int = 100,
        reasoning_effort: str | None = None,
        brave_api_key: str | None = None,
        web_proxy: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
    ):
        from nanobot.config.schema import ExecToolConfig
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.memory_window = memory_window
        self.reasoning_effort = reasoning_effort
        self.brave_api_key = brave_api_key
        self.web_proxy = web_proxy
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace

        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort=reasoning_effort,
            brave_api_key=brave_api_key,
            web_proxy=web_proxy,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._consolidating: set[str] = set()  # Session keys with consolidation in progress
        self._consolidation_tasks: set[asyncio.Task] = set()  # Strong refs to in-flight tasks
        self._consolidation_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._pending_withdrawals: dict[str, dict] = {}  # telegram_user_id -> {address, amount, expires}
        self._processing_lock = asyncio.Lock()
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
            path_append=self.exec_config.path_append,
        ))
        self.tools.register(WebSearchTool(api_key=self.brave_api_key, proxy=self.web_proxy))
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from nanobot.agent.tools.mcp import connect_mcp_servers
        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except Exception as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Update context for all tools that need routing info."""
        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""
        def _fmt(tc):
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop. Returns (final_content, tools_used, messages)."""
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []

        while iteration < self.max_iterations:
            iteration += 1

            response = await self.provider.chat_with_retry(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                reasoning_effort=self.reasoning_effort,
            )

            if response.has_tool_calls:
                if on_progress:
                    thought = self._strip_think(response.content)
                    if thought:
                        await on_progress(thought)
                    await on_progress(self._tool_hint(response.tool_calls), tool_hint=True)

                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False)
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tool_call.name, args_str[:200])
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                clean = self._strip_think(response.content)
                # Don't persist error responses to session history — they can
                # poison the context and cause permanent 400 loops (#1303).
                if response.finish_reason == "error":
                    logger.error("LLM returned error: {}", (clean or "")[:200])
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    break
                messages = self.context.add_assistant_message(
                    messages, clean, reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                final_content = clean
                break

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )

        return final_content, tools_used, messages

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        logger.info("FlagentBot agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if msg.content.strip().lower() == "/stop":
                await self._handle_stop(msg)
            else:
                task = asyncio.create_task(self._dispatch(msg))
                self._active_tasks.setdefault(msg.session_key, []).append(task)
                task.add_done_callback(lambda t, k=msg.session_key: self._active_tasks.get(k, []) and self._active_tasks[k].remove(t) if t in self._active_tasks.get(k, []) else None)

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        sub_cancelled = await self.subagents.cancel_by_session(msg.session_key)
        total = cancelled + sub_cancelled
        content = f"Stopped {total} task(s)." if total else "No active task to stop."
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message under the global lock."""
        async with self._processing_lock:
            try:
                response = await self._process_message(msg)
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="", metadata=msg.metadata or {},
                    ))
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Sorry, I encountered an error.",
                ))

    async def close_mcp(self) -> None:
        """Close MCP connections."""
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("FlagentBot agent loop stopping")

    # ---- User middleware (Supabase) -----------------------------------------

    async def _ensure_user(self, telegram_user_id: str, metadata: dict) -> dict:
        """Look up or create user in bot_users. Returns user row."""
        rows = await db.select(
            "bot_users",
            {"telegram_user_id": f"eq.{telegram_user_id}", "select": "*"},
        )
        if rows:
            return rows[0]
        # Create new user
        username = metadata.get("username", "")
        first_name = metadata.get("first_name", "")
        new_user = {
            "telegram_user_id": telegram_user_id,
            "telegram_username": username or "",
            "display_name": first_name or username or telegram_user_id,
            "flagent_balance": 0,
        }
        result = await db.insert("bot_users", new_user)
        return result[0] if result else new_user

    async def _check_balance(self, user: dict) -> bool:
        """Return True if user holds >= 25,000 $FLAGENT."""
        return (user.get("flagent_balance") or 0) >= _MIN_FLAGENT_HOLD

    async def _log_usage(
        self, telegram_user_id: str, action_type: str, detail: str = "",
    ) -> None:
        """Log action to bot_usage_log for analytics (no balance deduction)."""
        try:
            await db.insert("bot_usage_log", {
                "telegram_user_id": telegram_user_id,
                "action_type": action_type,
                "detail": detail[:500] if detail else "",
            })
        except Exception:
            logger.exception("Failed to log usage for user {}", telegram_user_id)

    # ---- /setup command ----------------------------------------------------

    async def _handle_setup(self, msg: InboundMessage, telegram_user_id: str) -> OutboundMessage:
        """Generate a BSC wallet and store encrypted private key in bot_users."""
        try:
            from eth_account import Account
        except ImportError:
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Wallet generation unavailable (eth_account not installed).",
            )

        # Check if user already has a wallet
        rows = await db.select(
            "bot_users",
            {"telegram_user_id": f"eq.{telegram_user_id}", "select": "wallet_address"},
        )
        if rows and rows[0].get("wallet_address"):
            addr = rows[0]["wallet_address"]
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=(
                    f"Your wallet: `{addr}`\n\n"
                    f"To start, send two things to this address:\n"
                    f"1. $FLAGENT — send at least 26,000 to account for the 3% transfer tax (you need 25,000 in your wallet to activate)\n"
                    f"   Buy here: {_PCS_SWAP_URL}\n"
                    f"2. BNB — for gas when trading (0.005 BNB is enough to start)\n\n"
                    f"After sending $FLAGENT, tap /deposit to refresh your balance."
                ),
                metadata={"buttons": [
                    ["Buy $FLAGENT", _PCS_SWAP_URL],
                    ["Check Balance", "/deposit"],
                ]},
            )

        # Generate new account
        acct = Account.create()
        wallet_address = acct.address
        private_key_hex = acct.key.hex()

        try:
            encrypted_key = _encrypt_private_key(private_key_hex)
        except RuntimeError as e:
            logger.error("Wallet setup failed: {}", e)
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Wallet setup failed — server configuration error.",
            )

        await db.update(
            "bot_users",
            {"wallet_address": wallet_address, "encrypted_private_key": encrypted_key},
            {"telegram_user_id": telegram_user_id},
        )

        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=(
                f"Your wallet: `{wallet_address}`\n\n"
                f"To start, send two things to this address:\n"
                f"1. $FLAGENT — send at least 26,000 to account for the 3% transfer tax (you need 25,000 in your wallet to activate)\n"
                f"   Buy here: {_PCS_SWAP_URL}\n"
                f"2. BNB — for gas when trading (0.005 BNB is enough to start)\n\n"
                f"After sending $FLAGENT, tap /deposit to refresh your balance."
            ),
            metadata={"buttons": [
                ["Buy $FLAGENT", _PCS_SWAP_URL],
                ["Check Balance", "/deposit"],
            ]},
        )

    # ---- /start command ----------------------------------------------------

    async def _handle_start(self, msg: InboundMessage, telegram_user_id: str) -> OutboundMessage:
        """Send the welcome message — returning users get a short version."""
        user = await self._ensure_user(telegram_user_id, msg.metadata or {})
        wallet = user.get("wallet_address")
        flagent_bal = user.get("flagent_balance") or 0

        # Returning user with wallet
        if wallet:
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=(
                    f"Welcome back to FlagentBot!\n\n"
                    f"Your wallet: `{wallet}`\n"
                    f"$FLAGENT balance: {flagent_bal:,.2f}\n\n"
                    f"Just talk to me — drop a CA to analyze, a wallet to research, or tell me to trade.\n\n"
                    f"/help for all commands"
                ),
                metadata={"buttons": [
                    ["Analyze Token", "How do I analyze a token?"],
                    ["Trade", "How do I trade?"],
                    ["Portfolio", "/positions"],
                ]},
            )

        # Returning user without wallet
        rows = await db.select("bot_users", {
            "telegram_user_id": f"eq.{telegram_user_id}", "select": "id",
        })
        if rows and not wallet:
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Welcome back! Run /setup to create your trading wallet.",
                metadata={"buttons": [
                    ["Setup Wallet", "/setup"],
                    ["What can I do?", "What can you do?"],
                ]},
            )

        # New user — full onboarding
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=(
                "Welcome to FlagentBot\n\n"
                "Your personal BSC assistant. Powered by Flagent's on-chain infrastructure.\n\n"
                "I can:\n"
                "- Analyze any token — drop a contract address\n"
                "- Deep dive any wallet — drop a wallet address\n"
                "- Trade on Four.Meme, Flap.sh, and PancakeSwap\n"
                "- Track your portfolio and set alerts\n\n"
                "Get started:\n"
                "1. Run /setup to create your trading wallet\n"
                f"2. Buy $FLAGENT on PancakeSwap and send at least 26,000 to your wallet (25,000 minimum + 3% transfer tax)\n"
                "3. Tap /deposit to refresh your balance\n"
                "4. Start asking me anything about BSC\n\n"
                f"$FLAGENT: `{_FLAGENT_CA}`\n"
                f"Buy here: {_PCS_SWAP_URL}\n\n"
                "/help for all commands"
            ),
            metadata={"buttons": [
                ["Setup Wallet", "/setup"],
                ["What can I do?", "What can you do?"],
            ]},
        )

    # ---- /deposit command --------------------------------------------------

    async def _handle_deposit(self, msg: InboundMessage, telegram_user_id: str) -> OutboundMessage:
        """Check on-chain $FLAGENT balance and update bot_users."""
        user = await self._ensure_user(telegram_user_id, msg.metadata or {})
        wallet = user.get("wallet_address")

        if not wallet:
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Run /setup first to create your wallet.",
            )

        try:
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider("https://bsc-dataseed.binance.org"))
            flagent_ca = Web3.to_checksum_address("0x1FF3506b0BC80c3CA027B6cEb7534FcfeDccFFFF")
            erc20_abi = [{
                "name": "balanceOf",
                "type": "function",
                "stateMutability": "view",
                "inputs": [{"name": "account", "type": "address"}],
                "outputs": [{"name": "", "type": "uint256"}],
            }, {
                "name": "decimals",
                "type": "function",
                "stateMutability": "view",
                "inputs": [],
                "outputs": [{"name": "", "type": "uint8"}],
            }]
            contract = w3.eth.contract(address=flagent_ca, abi=erc20_abi)
            raw_balance = contract.functions.balanceOf(Web3.to_checksum_address(wallet)).call()
            try:
                decimals = contract.functions.decimals().call()
            except Exception:
                decimals = 18
            balance = raw_balance / (10 ** decimals)

            # Update bot_users with on-chain balance
            await db.update(
                "bot_users",
                {"flagent_balance": balance},
                {"telegram_user_id": telegram_user_id},
            )

            # Check if this is first time reaching 25k (for quick-start message)
            prev_balance = user.get("flagent_balance") or 0
            first_activation = balance >= _MIN_FLAGENT_HOLD and prev_balance < _MIN_FLAGENT_HOLD

            if balance >= _MIN_FLAGENT_HOLD:
                deposit_msg = f"Balance: {balance:,.2f} $FLAGENT — Full access unlocked."
                buttons = [
                    ["Analyze Token", "Paste a token CA to analyze"],
                    ["Check Wallet", "Paste a wallet address to research"],
                    ["Portfolio", "/positions"],
                ]

                if first_activation:
                    deposit_msg += (
                        "\n\nYou're activated! Here's what you can do:\n\n"
                        "- Paste any token CA to analyze it\n"
                        "- 'Check wallet 0x...' to research a trader\n"
                        "- 'Buy 0.01 BNB of 0x...' to trade on Four.Meme\n"
                        "- 'What's trending on BSC?' for ecosystem research\n"
                        "- /positions to see your portfolio\n"
                        "- /balance to check your funds\n"
                        "- 'Alert me when 0xToken hits 0.001 BNB'\n\n"
                        "Or just talk to me naturally — I understand what you need."
                    )
                    buttons = [
                        ["Analyze Token", "Paste a token contract address"],
                        ["BSC Research", "What's happening on BSC today?"],
                        ["My Portfolio", "/positions"],
                    ]

                return OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content=deposit_msg,
                    metadata={"buttons": buttons},
                )
            elif balance > 0:
                needed = _MIN_FLAGENT_HOLD - balance
                deposit_msg = (
                    f"Balance: {balance:,.2f} $FLAGENT — You need at least 25,000 to activate. Send {needed:,.0f} more.\n\n"
                    f"Buy here: {_PCS_SWAP_URL}"
                )
            else:
                deposit_msg = f"Balance: 0 $FLAGENT — Send $FLAGENT to `{wallet}` to activate.\n\nBuy here: {_PCS_SWAP_URL}"

            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=deposit_msg,
            )
        except Exception as e:
            logger.exception("Failed to check $FLAGENT balance for {}", telegram_user_id)
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=f"Failed to check balance: {e}",
            )

    # ---- /balance command --------------------------------------------------

    async def _handle_balance(self, msg: InboundMessage, telegram_user_id: str) -> OutboundMessage:
        """Show BNB balance (from BSCScan) and $FLAGENT balance (from bot_users)."""
        user = await self._ensure_user(telegram_user_id, msg.metadata or {})
        wallet = user.get("wallet_address")
        flagent_balance = user.get("flagent_balance", 0) or 0

        if not wallet:
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Run /setup first to create your wallet.",
            )

        # Fetch BNB balance from BSCScan
        bnb_balance = 0.0
        try:
            import httpx
            bscscan_key = os.environ.get("BSCSCAN_API_KEY", "")
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get("https://api.bscscan.com/api", params={
                    "module": "account", "action": "balance",
                    "address": wallet, "tag": "latest", "apikey": bscscan_key,
                })
                data = resp.json()
                bnb_balance = int(data.get("result", "0")) / 1e18
        except Exception:
            logger.exception("Failed to fetch BNB balance for {}", wallet)

        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=(
                f"Wallet: `{wallet}`\n\n"
                f"BNB Balance: {bnb_balance:.4f} BNB\n"
                f"$FLAGENT Balance: {flagent_balance}"
            ),
        )

    # ---- /positions command ------------------------------------------------

    async def _handle_positions(self, msg: InboundMessage, telegram_user_id: str) -> OutboundMessage:
        """Show open positions from bot_positions."""
        user = await self._ensure_user(telegram_user_id, msg.metadata or {})
        if not user.get("wallet_address"):
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Run /setup first to create your wallet.",
            )

        rows = await db.select("bot_positions", {
            "telegram_user_id": f"eq.{telegram_user_id}",
            "order": "created_at.desc",
            "limit": "20",
        })

        if not rows:
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="No positions found. Start trading to see your portfolio here.",
            )

        lines = ["Your Positions:\n"]
        for i, p in enumerate(rows, 1):
            side = (p.get("side") or "?").upper()
            token = p.get("token_address", "?")
            short_token = token[:6] + "..." + token[-4:] if len(token) > 12 else token
            bnb = float(p.get("bnb_amount") or 0)
            platform = p.get("platform", "?")
            tx = (p.get("tx_hash") or "?")[:16]
            created = p.get("created_at", "")
            lines.append(f"{i}. {side} `{short_token}` | {bnb:.4f} BNB | {platform} | {tx}...")

        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content="\n".join(lines),
        )

    # ---- /withdraw command -------------------------------------------------

    async def _handle_withdraw(self, msg: InboundMessage, telegram_user_id: str) -> OutboundMessage:
        """Parse withdrawal request and ask for confirmation."""
        user = await self._ensure_user(telegram_user_id, msg.metadata or {})
        if not user.get("wallet_address"):
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Run /setup first to create your wallet.",
            )

        parts = msg.content.strip().split()
        if len(parts) != 3:
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Usage: /withdraw <address> <amount>\nExample: `/withdraw 0x1234...abcd 0.1`",
            )

        target_address = parts[1]
        try:
            amount = float(parts[2])
        except ValueError:
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Invalid amount. Use a number like 0.1",
            )

        if amount <= 0:
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Amount must be greater than 0.",
            )

        if not re.match(r'^0x[0-9a-fA-F]{40}$', target_address):
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Invalid BSC address format.",
            )

        encrypted_key = user.get("encrypted_private_key")
        if not encrypted_key:
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Run /setup first to create your wallet.",
            )

        # Store pending withdrawal — requires YES confirmation
        self._pending_withdrawals[telegram_user_id] = {
            "address": target_address,
            "amount": amount,
            "expires": time.time() + 60,
        }

        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=(
                f"Send {amount} BNB to `{target_address}`?\n"
                f"Reply YES to confirm. Expires in 60 seconds."
            ),
        )

    async def _execute_pending_withdrawal(self, msg: InboundMessage, telegram_user_id: str) -> OutboundMessage:
        """Execute a confirmed pending withdrawal."""
        pending = self._pending_withdrawals.pop(telegram_user_id)
        target_address = pending["address"]
        amount = pending["amount"]

        user = await self._ensure_user(telegram_user_id, msg.metadata or {})
        encrypted_key = user.get("encrypted_private_key")

        try:
            from web3 import Web3
            pk = _decrypt_private_key(encrypted_key)
            w3 = Web3(Web3.HTTPProvider("https://bsc-dataseed.binance.org"))
            account = w3.eth.account.from_key(pk)
            value_wei = w3.to_wei(amount, "ether")

            tx = {
                "to": Web3.to_checksum_address(target_address),
                "value": value_wei,
                "gas": 21_000,
                "gasPrice": w3.eth.gas_price,
                "nonce": w3.eth.get_transaction_count(account.address),
                "chainId": 56,
            }
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()

            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=(
                    f"Withdrawal sent!\n\n"
                    f"To: `{target_address}`\n"
                    f"Amount: {amount} BNB\n"
                    f"Tx: `{tx_hash}`"
                ),
            )
        except Exception as e:
            logger.exception("Withdraw failed for user {}", telegram_user_id)
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=f"Withdrawal failed: {e}",
            )

    # ---- /export_key command -----------------------------------------------

    async def _handle_export_key(self, msg: InboundMessage, telegram_user_id: str) -> OutboundMessage:
        """Decrypt and send the user's private key. Rate limited to once per 24h."""
        user = await self._ensure_user(telegram_user_id, msg.metadata or {})
        encrypted_key = user.get("encrypted_private_key")
        if not encrypted_key:
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Run /setup first to create your wallet.",
            )

        # Rate limit: once per 24 hours
        last_export = user.get("last_key_export")
        if last_export:
            try:
                last_dt = datetime.fromisoformat(last_export.replace("Z", "+00:00"))
                elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                if elapsed < 86400:
                    hours_left = int((86400 - elapsed) / 3600)
                    return OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content=f"Key export is rate limited to once per 24 hours. Try again in ~{hours_left}h.",
                    )
            except (ValueError, TypeError):
                pass

        try:
            pk = _decrypt_private_key(encrypted_key)
        except Exception:
            logger.exception("Failed to decrypt key for user {}", telegram_user_id)
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Failed to decrypt your key. Contact support.",
            )

        # Update last_key_export timestamp
        await db.update(
            "bot_users",
            {"last_key_export": datetime.now(timezone.utc).isoformat()},
            {"telegram_user_id": telegram_user_id},
        )

        # Send the key
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=(
                f"⚠️ This is your private key. Anyone with this can access your funds. "
                f"Import it into MetaMask or any BSC wallet. Never share it.\n\n"
                f"`{pk}`"
            ),
        ))

        # Send follow-up warning
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=(
                "⚠️ DELETE THIS MESSAGE after saving your key.\n"
                "Telegram messages are not end-to-end encrypted by default.\n"
                "Import into MetaMask or Trust Wallet, then delete the message above."
            ),
        )

    # ---- /withdraw_token command -------------------------------------------

    async def _handle_withdraw_token(self, msg: InboundMessage, telegram_user_id: str) -> OutboundMessage:
        """Parse token withdrawal request and ask for confirmation."""
        user = await self._ensure_user(telegram_user_id, msg.metadata or {})
        if not user.get("wallet_address"):
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Run /setup first to create your wallet.",
            )

        parts = msg.content.strip().split()
        if len(parts) != 4:
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=(
                    "Usage: /withdraw_token <token_address> <destination> <amount>\n"
                    "Example: `/withdraw_token 0xToken... 0xDest... 1000`\n"
                    "Use `all` to send your full balance."
                ),
            )

        token_address = parts[1]
        destination = parts[2]
        amount_str = parts[3]

        # Validate addresses
        addr_pattern = r'^0x[0-9a-fA-F]{40}$'
        if not re.match(addr_pattern, token_address):
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Invalid token address format. Must be 0x followed by 40 hex characters.",
            )
        if not re.match(addr_pattern, destination):
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Invalid destination address format. Must be 0x followed by 40 hex characters.",
            )

        encrypted_key = user.get("encrypted_private_key")
        if not encrypted_key:
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Run /setup first to create your wallet.",
            )

        # Resolve "all" to actual balance for confirmation message
        display_amount = amount_str
        symbol = "tokens"
        if amount_str.lower() in ("all", "100%"):
            try:
                from web3 import Web3
                w3 = Web3(Web3.HTTPProvider("https://bsc-dataseed.binance.org"))
                token_cs = Web3.to_checksum_address(token_address)
                erc20_abi = [
                    {"name": "balanceOf", "type": "function", "stateMutability": "view",
                     "inputs": [{"name": "account", "type": "address"}],
                     "outputs": [{"name": "", "type": "uint256"}]},
                    {"name": "symbol", "type": "function", "stateMutability": "view",
                     "inputs": [], "outputs": [{"name": "", "type": "string"}]},
                ]
                contract = w3.eth.contract(address=token_cs, abi=erc20_abi)
                bal = contract.functions.balanceOf(
                    Web3.to_checksum_address(user["wallet_address"])
                ).call()
                if bal == 0:
                    return OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="You hold 0 of this token. Nothing to withdraw.",
                    )
                display_amount = str(bal)
                try:
                    symbol = contract.functions.symbol().call()
                except Exception:
                    pass
            except Exception:
                display_amount = "all"
        else:
            # Try to get symbol for display
            try:
                from web3 import Web3
                w3 = Web3(Web3.HTTPProvider("https://bsc-dataseed.binance.org"))
                token_cs = Web3.to_checksum_address(token_address)
                sym_abi = [{"name": "symbol", "type": "function", "stateMutability": "view",
                            "inputs": [], "outputs": [{"name": "", "type": "string"}]}]
                contract = w3.eth.contract(address=token_cs, abi=sym_abi)
                symbol = contract.functions.symbol().call()
            except Exception:
                pass

        # Store pending withdrawal with type="token"
        self._pending_withdrawals[telegram_user_id] = {
            "type": "token",
            "token_address": token_address,
            "address": destination,
            "amount": amount_str,
            "expires": time.time() + 60,
        }

        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=(
                f"Send {display_amount} {symbol} to `{destination}`?\n"
                f"Token: `{token_address}`\n"
                f"Reply YES to confirm. Expires in 60 seconds."
            ),
        )

    async def _execute_pending_token_withdrawal(self, msg: InboundMessage, telegram_user_id: str) -> OutboundMessage:
        """Execute a confirmed pending token withdrawal via the withdraw_token script."""
        pending = self._pending_withdrawals.pop(telegram_user_id)
        token_address = pending["token_address"]
        destination = pending["address"]
        amount = pending["amount"]

        import subprocess
        import sys as _sys
        from pathlib import Path

        script = Path(self.workspace) / "skills" / "withdraw_token" / "scripts" / "withdraw_token.py"
        if not script.exists():
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Token withdrawal script not found. Contact support.",
            )

        try:
            result = subprocess.run(
                [_sys.executable, str(script), telegram_user_id, token_address, destination, amount],
                capture_output=True, text=True, timeout=60,
                env={**os.environ},
            )
            output = result.stdout.strip()
            if result.returncode != 0:
                err = result.stderr[:300] if result.stderr else "Withdrawal script failed"
                return OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content=f"Token withdrawal failed: {err}",
                )

            try:
                data = json.loads(output)
            except json.JSONDecodeError:
                return OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content=output or "Withdrawal completed but no output received.",
                )

            if data.get("status") == "error":
                return OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content=data.get("message", "Withdrawal failed."),
                )

            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=(
                    f"Token withdrawal sent!\n\n"
                    f"Token: {data.get('symbol', '?')} (`{token_address}`)\n"
                    f"Amount: {data.get('amount', '?')}\n"
                    f"To: `{destination}`\n"
                    f"Tx: `{data.get('tx_hash', '?')}`"
                ),
                metadata={"buttons": [
                    ["View Balance", "/balance"],
                    ["Portfolio", "/positions"],
                ]},
            )
        except subprocess.TimeoutExpired:
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Token withdrawal timed out. BSC may be congested — try again.",
            )
        except Exception as e:
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=f"Token withdrawal failed: {e}",
            )

    # ---- /usage command ----------------------------------------------------

    async def _handle_usage(self, msg: InboundMessage, telegram_user_id: str) -> OutboundMessage:
        """Show $FLAGENT spending history from bot_usage_log."""
        rows = await db.select("bot_usage_log", {
            "telegram_user_id": f"eq.{telegram_user_id}",
            "order": "created_at.desc",
            "limit": "100",
        })

        if not rows:
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="No usage history yet.",
            )

        # Aggregate by action type
        totals: dict[str, int] = {}
        for r in rows:
            action = r.get("action_type", "unknown")
            cost = int(r.get("cost") or 0)
            totals[action] = totals.get(action, 0) + cost

        grand_total = sum(totals.values())

        lines = ["$FLAGENT Usage:\n"]
        for action, total in sorted(totals.items(), key=lambda x: -x[1]):
            lines.append(f"  {action}: {total} $FLAGENT")
        lines.append(f"\nTotal consumed: {grand_total} $FLAGENT")
        lines.append(f"Transactions: {len(rows)}")

        user = await self._ensure_user(telegram_user_id, msg.metadata or {})
        balance = user.get("flagent_balance", 0) or 0
        lines.append(f"Current balance: {balance} $FLAGENT")

        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content="\n".join(lines),
        )

    # ---- /help command -----------------------------------------------------

    async def _handle_help(self, msg: InboundMessage) -> OutboundMessage:
        """Show all commands."""
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=(
                "FlagentBot Commands:\n\n"
                "/start — Meet your BSC assistant\n"
                "/setup — Create your trading wallet\n"
                "/deposit — Refresh your $FLAGENT balance\n"
                "/balance — Check your BNB + $FLAGENT balance\n"
                "/positions — View your open trades\n"
                "/withdraw — Withdraw BNB (usage: /withdraw 0xAddress 0.1)\n"
                "/withdraw_token — Send tokens to external wallet (usage: /withdraw_token 0xToken 0xDest amount)\n"
                "/export_key — Export your wallet private key\n"
                "/usage — See your $FLAGENT spending history\n"
                "/help — Show this message\n"
                "/new — Start a fresh conversation\n\n"
                "Or just talk to me — drop a contract address to analyze a token, "
                "drop a wallet to analyze a trader, or tell me to buy/sell."
            ),
        )

    # ---- Main message processing -------------------------------------------

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
            history = session.get_history(max_messages=self.memory_window)
            # System messages don't have per-user memory context
            self.context.set_memory(MemoryStore(None))
            messages = await self.context.build_messages(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
            )
            final_content, _, all_msgs = await self._run_agent_loop(messages)
            self._save_turn(session, all_msgs, 1 + len(history))
            self.sessions.save(session)
            return OutboundMessage(channel=channel, chat_id=chat_id,
                                  content=final_content or "Background task completed.")

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        # ---- User middleware: extract ID, ensure user, check balance --------
        telegram_user_id = _parse_telegram_user_id(msg.sender_id)
        user = await self._ensure_user(telegram_user_id, msg.metadata or {})

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)

        # ---- Pending withdrawal confirmation -----------------------------------
        raw_text = msg.content.strip()
        if telegram_user_id in self._pending_withdrawals:
            pending = self._pending_withdrawals[telegram_user_id]
            if time.time() > pending["expires"]:
                self._pending_withdrawals.pop(telegram_user_id, None)
                return OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Withdrawal expired. Run /withdraw again.",
                )
            if raw_text.upper() == "YES":
                if pending.get("type") == "token":
                    return await self._execute_pending_token_withdrawal(msg, telegram_user_id)
                return await self._execute_pending_withdrawal(msg, telegram_user_id)
            else:
                # Any other message cancels the pending withdrawal silently
                self._pending_withdrawals.pop(telegram_user_id, None)

        # Slash commands
        cmd = raw_text.lower()
        cmd_word = cmd.split()[0] if cmd else ""

        if cmd == "/start":
            return await self._handle_start(msg, telegram_user_id)

        if cmd == "/setup":
            return await self._handle_setup(msg, telegram_user_id)

        if cmd == "/deposit":
            return await self._handle_deposit(msg, telegram_user_id)

        if cmd == "/balance":
            return await self._handle_balance(msg, telegram_user_id)

        if cmd == "/positions":
            return await self._handle_positions(msg, telegram_user_id)

        if cmd_word == "/withdraw_token":
            return await self._handle_withdraw_token(msg, telegram_user_id)

        if cmd_word == "/withdraw":
            return await self._handle_withdraw(msg, telegram_user_id)

        if cmd == "/export_key":
            return await self._handle_export_key(msg, telegram_user_id)

        if cmd == "/usage":
            return await self._handle_usage(msg, telegram_user_id)

        if cmd == "/new":
            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())
            self._consolidating.add(session.key)
            try:
                async with lock:
                    snapshot = session.messages[session.last_consolidated:]
                    if snapshot:
                        temp = Session(key=session.key)
                        temp.messages = list(snapshot)
                        memory = MemoryStore(telegram_user_id)
                        if not await memory.consolidate(temp, self.provider, self.model, archive_all=True):
                            return OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content="Memory archival failed, session not cleared. Please try again.",
                            )
            except Exception:
                logger.exception("/new archival failed for {}", session.key)
                return OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Memory archival failed, session not cleared. Please try again.",
                )
            finally:
                self._consolidating.discard(session.key)

            session.clear()
            self.sessions.save(session)
            self.sessions.invalidate(session.key)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="New session started.")
        if cmd == "/help":
            return await self._handle_help(msg)

        # ---- Balance check --------------------------------------------------
        if not await self._check_balance(user):
            wallet_addr = user.get("wallet_address", "")
            if wallet_addr:
                gate_msg = (
                    "You need to hold at least 25,000 $FLAGENT to use me.\n"
                    "Send at least 26,000 to cover the 3% transfer tax.\n\n"
                    f"Buy on PancakeSwap: {_PCS_SWAP_URL}\n"
                    f"Send to your wallet: `{wallet_addr}`\n"
                    "Then tap /deposit to refresh."
                )
            else:
                gate_msg = (
                    "You need to hold at least 25,000 $FLAGENT to use me.\n"
                    "Send at least 26,000 to cover the 3% transfer tax.\n\n"
                    f"Buy on PancakeSwap: {_PCS_SWAP_URL}\n\n"
                    "No wallet yet? Run /setup first."
                )
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=gate_msg,
            )

        # ---- Memory consolidation trigger -----------------------------------
        unconsolidated = len(session.messages) - session.last_consolidated
        if (unconsolidated >= self.memory_window and session.key not in self._consolidating):
            self._consolidating.add(session.key)
            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())

            _user_id = telegram_user_id  # capture for closure

            async def _consolidate_and_unlock():
                try:
                    async with lock:
                        memory = MemoryStore(_user_id)
                        await memory.consolidate(
                            session, self.provider, self.model,
                            memory_window=self.memory_window,
                        )
                finally:
                    self._consolidating.discard(session.key)
                    _task = asyncio.current_task()
                    if _task is not None:
                        self._consolidation_tasks.discard(_task)

            _task = asyncio.create_task(_consolidate_and_unlock())
            self._consolidation_tasks.add(_task)

        # ---- Build context and run agent loop --------------------------------
        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"))
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        # Set per-user memory for context building
        user_memory = MemoryStore(telegram_user_id)
        self.context.set_memory(user_memory)

        history = session.get_history(max_messages=self.memory_window)
        initial_messages = await self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel, chat_id=msg.chat_id,
        )

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=meta,
            ))

        final_content, tools_used, all_msgs = await self._run_agent_loop(
            initial_messages, on_progress=on_progress or _bus_progress,
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        self._save_turn(session, all_msgs, 1 + len(history))
        self.sessions.save(session)

        # ---- Usage logging (analytics only, no deduction) --------------------
        action_type = "tool_use" if tools_used else "chat"
        await self._log_usage(
            telegram_user_id, action_type,
            detail=f"tools={','.join(tools_used[:5])}" if tools_used else "",
        )

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=final_content,
            metadata=msg.metadata or {},
        )

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool" and isinstance(content, str) and len(content) > self._TOOL_RESULT_MAX_CHARS:
                entry["content"] = content[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
            elif role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    # Strip the runtime-context prefix, keep only the user text.
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        continue
                if isinstance(content, list):
                    filtered = []
                    for c in content:
                        if c.get("type") == "text" and isinstance(c.get("text"), str) and c["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                            continue  # Strip runtime context from multimodal messages
                        if (c.get("type") == "image_url"
                                and c.get("image_url", {}).get("url", "").startswith("data:image/")):
                            filtered.append({"type": "text", "text": "[image]"})
                        else:
                            filtered.append(c)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    async def _consolidate_memory(self, session, telegram_user_id: str | None = None, archive_all: bool = False) -> bool:
        """Delegate to MemoryStore.consolidate(). Returns True on success."""
        memory = MemoryStore(telegram_user_id)
        return await memory.consolidate(
            session, self.provider, self.model,
            archive_all=archive_all, memory_window=self.memory_window,
        )

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Process a message directly (for CLI or cron usage)."""
        await self._connect_mcp()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        response = await self._process_message(msg, session_key=session_key, on_progress=on_progress)
        return response.content if response else ""
