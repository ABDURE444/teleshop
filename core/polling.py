"""Cancellable long-polling loop for a single Bot fed into a shared Dispatcher.

This is the primitive that makes single-process multibot work: instead of
`dp.start_polling()` (which owns the whole process and can't add/remove bots
at runtime), each bot gets one of these loops as an asyncio task. Stopping a
shop bot == cancelling its task. Updates are dispatched via the public
`dp.feed_update()` API.
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)

logger = logging.getLogger(__name__)

POLL_TIMEOUT = 25  # seconds; Telegram long-poll wait


async def poll_bot(dp: Dispatcher, bot: Bot, *, name: str = "", on_unauthorized=None) -> None:
    """Run a get_updates loop for `bot`, dispatching into `dp`.

    Runs until cancelled. If Telegram reports the token as invalid/revoked,
    `on_unauthorized` (async callable) is invoked once and the loop exits.
    """
    offset: int | None = None
    backoff = 1.0
    in_flight: set[asyncio.Task] = set()
    label = name or f"bot:{bot.id}"

    logger.info("[POLL:%s] polling started", label)
    try:
        while True:
            try:
                updates = await bot.get_updates(offset=offset, timeout=POLL_TIMEOUT)
                backoff = 1.0
                for update in updates:
                    offset = update.update_id + 1
                    task = asyncio.create_task(_feed(dp, bot, update, label))
                    in_flight.add(task)
                    task.add_done_callback(in_flight.discard)
            except asyncio.CancelledError:
                raise
            except TelegramUnauthorizedError:
                logger.error("[POLL:%s] token is invalid or revoked — stopping this bot", label)
                if on_unauthorized:
                    try:
                        await on_unauthorized()
                    except Exception:
                        logger.exception("[POLL:%s] on_unauthorized callback failed", label)
                return
            except TelegramRetryAfter as e:
                logger.warning("[POLL:%s] rate limited, sleeping %ss", label, e.retry_after)
                await asyncio.sleep(e.retry_after)
            except (TelegramNetworkError, ConnectionError, TimeoutError) as e:
                logger.warning("[POLL:%s] network error: %s — retrying in %.1fs", label, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except Exception:
                logger.exception("[POLL:%s] unexpected polling error — retrying in %.1fs", label, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
    finally:
        # Let handlers that are mid-flight finish briefly, then let go.
        if in_flight:
            await asyncio.wait(in_flight, timeout=5)
        logger.info("[POLL:%s] polling stopped", label)


async def _feed(dp: Dispatcher, bot: Bot, update, label: str) -> None:
    try:
        await dp.feed_update(bot, update)
    except Exception:
        logger.exception("[POLL:%s] handler error for update %s", label, update.update_id)
