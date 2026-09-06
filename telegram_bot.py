from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import timezone
from typing import Any, Callable, Dict, Optional

from bot.process_lock import ProcessLock
from dashboard_state import DashboardState, TradeRecord
from telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)
_telegram_polling_lock: ProcessLock | None = None

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler
except Exception:  # pragma: no cover - formatter tests do not need Telegram installed.
    InlineKeyboardButton = InlineKeyboardMarkup = Update = None  # type: ignore[assignment]
    Application = CallbackQueryHandler = CommandHandler = None  # type: ignore[assignment]


def _code_block(text: str) -> str:
    return "```\n" + str(text).replace("\\", "\\\\").replace("`", "\\`") + "\n```"


def _fmt_money(value: Optional[float], signed: bool = False) -> str:
    value = float(value or 0.0)
    return f"{value:+.2f}" if signed else f"{value:.2f}"


def _fmt_price(value: Optional[float], decimals: int = 2) -> str:
    return "NA" if value is None else f"{float(value):,.{decimals}f}"


def _fmt_hold(seconds: Optional[float]) -> str:
    total = max(0, int(seconds or 0))
    return f"{total // 60}m {total % 60}s"


def _trade_pnl(trade: TradeRecord) -> Optional[float]:
    cost = float(trade.entry_price or 0.0) * float(trade.qty or 0.0)
    if trade.redeem_amount is not None:
        return float(trade.redeem_amount) - cost
    if trade.exit_price is not None:
        return (float(trade.exit_price) - float(trade.entry_price or 0.0)) * float(trade.qty or 0.0)
    return None


def render_help() -> str:
    return (
        "1. /status - bot, position, balances\n"
        "2. /position - current position details\n"
        "3. /pause - pause new orders\n"
        "4. /resume - resume new orders\n"
        "5. /flatten - request market flatten with confirmation\n"
        "6. /errors - last 5 errors\n"
        "7. /trades - last 10 trades\n"
        "8. /pnl - realized PnL summary"
    )


def render_status(state: DashboardState) -> str:
    state = state.snapshot() if hasattr(state, "snapshot") else state
    paused = bool(state.bot_paused)
    bot_line = "🔴 Bot: PAUSED" if paused else "🟢 Bot: RUNNING"
    side = state.position_side or "NO POSITION"
    spread = float(state.spot_price or 0.0) - float(state.strike_price or 0.0)
    spread_mark = "✅" if spread >= 0 else "❌"
    lines = [
        bot_line,
        f"PnL today:  ${_fmt_money(state.visible_trades_pnl, signed=True)}",
        f"Position:   {side} @ {_fmt_price(state.position_entry, 2)} ({_fmt_price(state.position_qty, 2)} shares)",
        f"Hold:       {_fmt_hold(getattr(state, 'position_hold_sec', None))}",
        f"Strike:     ${_fmt_price(state.strike_price, 0)}",
        f"Spot:       ${_fmt_price(state.spot_price, 0)} ({spread:+.0f}) {spread_mark}",
        f"USDC:       ${_fmt_money(state.usdc_balance)}",
        f"POL:        {_fmt_price(state.pol_balance, 4)}",
    ]
    return _code_block("\n".join(lines))


def render_position(state: DashboardState) -> str:
    state = state.snapshot() if hasattr(state, "snapshot") else state
    side = state.position_side or "NO POSITION"
    unrealized = 0.0
    if state.position_entry is not None and state.position_qty:
        unrealized = (float(state.current_market_price or 0.0) - float(state.position_entry)) * float(state.position_qty)
    slug = getattr(state, "current_market_slug", None) or getattr(state, "market_slug", None) or "-"
    lines = [
        f"Side:          {side}",
        f"Entry:         {_fmt_price(state.position_entry, 4)}",
        f"Qty:           {_fmt_price(state.position_qty, 4)}",
        f"Market price:  {_fmt_price(state.current_market_price, 4)}",
        f"Unrealized:    ${_fmt_money(unrealized, signed=True)}",
        f"Target ask:    {_fmt_price(state.position_ask, 4)}",
        f"Market slug:   {slug}",
    ]
    return _code_block("\n".join(lines))


def render_trades(state: DashboardState) -> str:
    state = state.snapshot() if hasattr(state, "snapshot") else state
    lines = ["# | slug         | side | entry | exit | redeem | pnl"]
    for idx, trade in enumerate(list(state.trades)[:10], start=1):
        slug = str(trade.market_slug)[-12:]
        pnl = _trade_pnl(trade)
        exit_text = f"{float(trade.exit_price):.2f}" if trade.exit_price is not None else "NA"
        redeem_text = f"{float(trade.redeem_amount):.2f}" if trade.redeem_amount is not None else "NA"
        pnl_text = f"{pnl:+.2f}" if pnl is not None else "NA"
        lines.append(
            f"{idx:<1} | {slug:<12} | {trade.side:<4} | "
            f"{float(trade.entry_price):.2f} | {exit_text} | {redeem_text} | {pnl_text}"
        )
    return _code_block("\n".join(lines))


def render_pnl(state: DashboardState) -> str:
    state = state.snapshot() if hasattr(state, "snapshot") else state
    settled = [t for t in state.trades if _trade_pnl(t) is not None]
    pnls = [_trade_pnl(t) or 0.0 for t in settled]
    wins = sum(1 for pnl in pnls if pnl > 0)
    total = len(pnls)
    win_rate = (wins / total * 100.0) if total else 0.0
    lines = [
        f"Cumulative PnL: ${_fmt_money(state.cumulative_pnl, signed=True)}",
        f"Today PnL:      ${_fmt_money(state.visible_trades_pnl, signed=True)}",
        f"Win rate:       {win_rate:.1f}% ({wins}/{total})",
        f"Largest win:    ${_fmt_money(max(pnls) if pnls else 0.0, signed=True)}",
        f"Largest loss:   ${_fmt_money(min(pnls) if pnls else 0.0, signed=True)}",
    ]
    return _code_block("\n".join(lines))


def render_errors(state: DashboardState) -> str:
    state = state.snapshot() if hasattr(state, "snapshot") else state
    if not state.recent_errors:
        return TelegramNotifier.escape_md("✅ No recent errors")
    lines = []
    for ts, message in list(state.recent_errors)[-5:]:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        lines.append(f"{ts.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S UTC}  {str(message)[:200]}")
    return _code_block("\n".join(lines))


class TelegramBotController:
    def __init__(self, state: DashboardState) -> None:
        self.state = state
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        raw_chat_id = os.getenv("TELEGRAM_OWNER_CHAT_ID", "")
        self.owner_chat_id = int(raw_chat_id) if raw_chat_id.strip() else None
        self.pending_flatten: Dict[int, float] = {}
        if Application is None:
            self.application = None
        elif not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
        else:
            self.application = Application.builder().token(self.token).build()
            self._register_handlers()

    def start_background_thread(self) -> threading.Thread:
        thread = threading.Thread(target=lambda: asyncio.run(self.run()), daemon=True, name="telegram-bot")
        thread.start()
        return thread

    async def run(self) -> None:
        if self.application is None:
            raise RuntimeError("python-telegram-bot v20+ is required")
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("Telegram bot polling started")
        await asyncio.Event().wait()

    def _register_handlers(self) -> None:
        commands: Dict[str, Callable[[Any, Any], Any]] = {
            "start": self._help,
            "help": self._help,
            "status": self._status,
            "pause": self._pause,
            "resume": self._resume,
            "flatten": self._flatten,
            "position": self._position,
            "trades": self._trades,
            "pnl": self._pnl,
            "errors": self._errors,
        }
        for name, handler in commands.items():
            self.application.add_handler(CommandHandler(name, handler))
        self.application.add_handler(CallbackQueryHandler(self._button))

    def _is_authorized(self, update: Any) -> bool:
        chat = update.effective_chat
        if self.owner_chat_id is not None and chat and int(chat.id) == self.owner_chat_id:
            return True
        chat_id = getattr(chat, "id", None)
        logger.warning("Unauthorized Telegram chat id: %s", chat_id)
        return False

    async def _reject(self, update: Any) -> None:
        if update.callback_query:
            await update.callback_query.answer("Unauthorized", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("Unauthorized")

    def _menu(self) -> Any:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Status", callback_data="status"), InlineKeyboardButton("📍 Position", callback_data="position")],
            [InlineKeyboardButton("⏸ Pause", callback_data="pause"), InlineKeyboardButton("▶️ Resume", callback_data="resume")],
            [InlineKeyboardButton("📜 Errors", callback_data="errors"), InlineKeyboardButton("📈 Trades", callback_data="trades")],
            [InlineKeyboardButton("💰 PnL", callback_data="pnl"), InlineKeyboardButton("🆘 Flatten", callback_data="flatten")],
        ])

    async def _reply(self, update: Any, text: str, reply_markup: Any = None) -> None:
        if update.callback_query:
            await update.callback_query.edit_message_text(text=text, parse_mode="MarkdownV2", reply_markup=reply_markup)
        else:
            await update.effective_message.reply_text(text=text, parse_mode="MarkdownV2", reply_markup=reply_markup)

    async def _help(self, update: Any, context: Any) -> None:
        if not self._is_authorized(update):
            await self._reject(update)
            return
        await self._reply(update, _code_block(render_help()), reply_markup=self._menu())

    async def _status(self, update: Any, context: Any) -> None:
        await self._command(update, render_status)

    async def _position(self, update: Any, context: Any) -> None:
        await self._command(update, render_position)

    async def _trades(self, update: Any, context: Any) -> None:
        await self._command(update, render_trades)

    async def _pnl(self, update: Any, context: Any) -> None:
        await self._command(update, render_pnl)

    async def _errors(self, update: Any, context: Any) -> None:
        await self._command(update, render_errors)

    async def _command(self, update: Any, renderer: Callable[[DashboardState], str]) -> None:
        if not self._is_authorized(update):
            await self._reject(update)
            return
        await self._reply(update, renderer(self.state), reply_markup=self._menu())

    async def _pause(self, update: Any, context: Any) -> None:
        if not self._is_authorized(update):
            await self._reject(update)
            return
        self.state.update(bot_paused=True)
        await self._reply(update, TelegramNotifier.escape_md("⏸ Bot paused. New orders blocked. Use /resume to restart."), reply_markup=self._menu())

    async def _resume(self, update: Any, context: Any) -> None:
        if not self._is_authorized(update):
            await self._reject(update)
            return
        self.state.update(bot_paused=False)
        await self._reply(update, TelegramNotifier.escape_md("▶️ Bot resumed."), reply_markup=self._menu())

    async def _flatten(self, update: Any, context: Any) -> None:
        if not self._is_authorized(update):
            await self._reject(update)
            return
        chat_id = int(update.effective_chat.id)
        self.pending_flatten[chat_id] = time.time() + 30.0
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm flatten", callback_data="flatten_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="flatten_cancel"),
        ]])
        await self._reply(update, TelegramNotifier.escape_md("Confirm flatten current position? Expires in 30 seconds."), reply_markup=keyboard)

    async def _button(self, update: Any, context: Any) -> None:
        query = update.callback_query
        if not self._is_authorized(update):
            await self._reject(update)
            return
        await query.answer()
        data = query.data
        if data == "flatten_confirm":
            expires_at = self.pending_flatten.get(int(update.effective_chat.id), 0.0)
            if time.time() > expires_at:
                await self._reply(update, TelegramNotifier.escape_md("Flatten confirmation expired."), reply_markup=self._menu())
                return
            self.pending_flatten.pop(int(update.effective_chat.id), None)
            self.state.update(flatten_requested=True)
            await self._reply(update, TelegramNotifier.escape_md("🔄 Flatten requested. Monitoring position..."), reply_markup=self._menu())
            return
        if data == "flatten_cancel":
            self.pending_flatten.pop(int(update.effective_chat.id), None)
            await self._reply(update, TelegramNotifier.escape_md("Flatten cancelled."), reply_markup=self._menu())
            return
        handlers = {
            "status": self._status,
            "position": self._position,
            "pause": self._pause,
            "resume": self._resume,
            "flatten": self._flatten,
            "errors": self._errors,
            "trades": self._trades,
            "pnl": self._pnl,
        }
        handler = handlers.get(data)
        if handler is not None:
            await handler(update, context)


def start_telegram_bot_thread(state: DashboardState) -> Optional[threading.Thread]:
    global _telegram_polling_lock
    if os.getenv("TELEGRAM_CONTROLLER_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        logger.info("Telegram bot controller disabled by TELEGRAM_CONTROLLER_ENABLED")
        return None
    if Application is None:
        logger.warning("Telegram bot disabled; install python-telegram-bot>=20")
        return None
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_OWNER_CHAT_ID"):
        logger.warning("Telegram bot disabled; TELEGRAM_BOT_TOKEN or TELEGRAM_OWNER_CHAT_ID missing")
        return None
    if _telegram_polling_lock is None:
        lock_path = os.getenv(
            "TELEGRAM_POLLING_LOCK_PATH",
            "/tmp/hyperliquid-outcome-telegram-polling.lock",
        )
        lock = ProcessLock(lock_path)
        if not lock.acquire():
            logger.warning(
                "Telegram bot controller not started: another local process holds the polling lock."
            )
            return None
        _telegram_polling_lock = lock
    try:
        return TelegramBotController(state).start_background_thread()
    except Exception:
        if _telegram_polling_lock is not None:
            _telegram_polling_lock.release()
            _telegram_polling_lock = None
        raise
