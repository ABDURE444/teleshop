"""Injects shop context into every shop-bot update.

All shop bots share ONE Dispatcher; this middleware asks the BotManager
which shop the receiving Bot belongs to and provides `shop_id` to handlers.
Updates from bots the manager doesn't know (e.g. mid-shutdown races) are
dropped safely.
"""
import logging

from aiogram import BaseMiddleware

logger = logging.getLogger(__name__)


class ShopContextMiddleware(BaseMiddleware):
    def __init__(self, ctx):
        self.ctx = ctx

    async def __call__(self, handler, event, data):
        bot = data.get("bot")
        if bot is None:
            return await handler(event, data)
        shop_id = self.ctx.bot_manager.shop_id_for_bot(bot.id)
        if not shop_id:
            logger.warning("[SHOPCTX] update for unknown bot_id=%s dropped", bot.id)
            return None
        data["shop_id"] = shop_id
        return await handler(event, data)
