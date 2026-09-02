"""In-process lifecycle manager for shop bots.

Replaces v1's subprocess architecture (generated per-shop scripts, PID
tracking, pgrep/pkill, per-process DB pools). Every shop bot is now:

  * one aiogram `Bot` instance (its own token),
  * one asyncio polling task (core/polling.py),
  * dispatched through ONE shared shop Dispatcher, sharing the app's single
    DB engine and Redis connection.

The manager also maintains the bot_id -> shop_id map that the shop context
middleware uses to know which shop an incoming update belongs to.
"""
import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError

from core.polling import poll_bot

logger = logging.getLogger(__name__)


@dataclass
class RunningShop:
    shop_id: str
    bot: Bot
    task: asyncio.Task
    bot_username: str | None = None


class BotManager:
    def __init__(self, shop_dp: Dispatcher):
        self.shop_dp = shop_dp
        self._running: dict[str, RunningShop] = {}       # shop_id -> RunningShop
        self._shop_by_bot_id: dict[int, str] = {}        # bot.id -> shop_id
        self._lock = asyncio.Lock()
        # Optional async callback: called as on_token_invalid(shop_id) when
        # Telegram rejects a shop's token mid-flight (revoked at BotFather).
        self.on_token_invalid = None

    # ---------------------------------------------------------------- lookup

    def shop_id_for_bot(self, bot_id: int) -> str | None:
        return self._shop_by_bot_id.get(bot_id)

    def bot_for_shop(self, shop_id: str):
        """The live Bot instance for a shop, or None if it isn't running.

        The dashboard uses this to push uploaded photos through the Bot API
        so Telegram (not this server) stores and serves them.
        """
        rs = self._running.get(shop_id)
        return rs.bot if rs is not None and not rs.task.done() else None

    def is_running(self, shop_id: str) -> bool:
        rs = self._running.get(shop_id)
        return rs is not None and not rs.task.done()

    def running_shop_ids(self) -> list[str]:
        return [sid for sid, rs in self._running.items() if not rs.task.done()]

    # ------------------------------------------------------------- lifecycle

    async def start_shop(self, shop_id: str, token: str) -> tuple[bool, str]:
        """Start (or restart) a shop bot. Returns (ok, bot_username_or_error)."""
        async with self._lock:
            if self.is_running(shop_id):
                rs = self._running[shop_id]
                return True, rs.bot_username or ""

            # Clean up any dead entry before starting fresh
            await self._teardown(shop_id)

            bot = Bot(token=token)
            try:
                me = await bot.get_me()
            except TelegramUnauthorizedError:
                await bot.session.close()
                logger.error("[MANAGER] invalid token for shop %s", shop_id)
                return False, "invalid_token"
            except Exception as e:
                await bot.session.close()
                logger.error("[MANAGER] could not reach Telegram for shop %s: %s", shop_id, e)
                return False, f"telegram_error: {e}"

            # Drop any webhook the token may have configured elsewhere and
            # discard stale updates queued while the shop was offline.
            try:
                await bot.delete_webhook(drop_pending_updates=True)
            except Exception as e:
                logger.warning("[MANAGER] delete_webhook failed for shop %s: %s", shop_id, e)

            async def _on_unauthorized():
                logger.error("[MANAGER] token revoked at runtime for shop %s", shop_id)
                await self._teardown(shop_id)
                if self.on_token_invalid:
                    try:
                        await self.on_token_invalid(shop_id)
                    except Exception:
                        logger.exception("[MANAGER] on_token_invalid callback failed for %s", shop_id)

            task = asyncio.create_task(
                poll_bot(self.shop_dp, bot, name=f"shop:{shop_id}", on_unauthorized=_on_unauthorized),
                name=f"shopbot:{shop_id}",
            )
            self._running[shop_id] = RunningShop(shop_id=shop_id, bot=bot, task=task, bot_username=me.username)
            self._shop_by_bot_id[bot.id] = shop_id
            logger.info("[MANAGER] shop %s started as @%s (bot_id=%s)", shop_id, me.username, bot.id)
            return True, me.username or ""

    async def stop_shop(self, shop_id: str) -> bool:
        async with self._lock:
            return await self._teardown(shop_id)

    async def _teardown(self, shop_id: str) -> bool:
        rs = self._running.pop(shop_id, None)
        if not rs:
            return False
        if not rs.task.done():
            rs.task.cancel()
            try:
                await rs.task
            except (asyncio.CancelledError, Exception):
                pass
        self._shop_by_bot_id.pop(rs.bot.id, None)
        try:
            await rs.bot.session.close()
        except Exception:
            pass
        logger.info("[MANAGER] shop %s stopped", shop_id)
        return True

    async def stop_all(self) -> None:
        for shop_id in list(self._running.keys()):
            await self.stop_shop(shop_id)
