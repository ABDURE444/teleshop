"""Background tasks (asyncio, in-process).

v1 had five loops, two of which existed only to babysit OS subprocesses.
v2 keeps the business loops and replaces process monitoring with a single
consistency check against the BotManager.
"""
import asyncio
import logging
from datetime import timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from context import AppContext
from services.shop_service import utcnow

logger = logging.getLogger(__name__)

EXPIRATION_INTERVAL = 3600        # 1h
REMINDER_INTERVAL = 6 * 3600      # 6h
AFFPAY_MONITOR_INTERVAL = 30      # 30s
CONSISTENCY_INTERVAL = 300        # 5min
TOPUP_SWEEP_INTERVAL = 300        # 5min — cancel part-paid orders that stalled
REMINDER_WINDOW_DAYS = 7
MAX_AFFPAY_RETRIES = 5


class BackgroundTasks:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self._tasks: list[asyncio.Task] = []

    async def sweep_topups(self):
        """Cancel part-paid orders whose balance never arrived; tell both sides.

        The platform never holds the money, so the only honest thing it can do
        is cancel, record what the shop owes back, and make sure nobody is left
        waiting silently.
        """
        from core.i18n import get_lang, t
        async with self.ctx.db() as session:
            cancelled = await self.ctx.orders.sweep_abandoned_topups(session)
            for order in cancelled:
                admin_ids = await self.ctx.orders.get_admin_ids(session, order.shop_id)
                items_owner = (order.shop_id, admin_ids)
                bot = self.ctx.bot_manager.bot_for_shop(order.shop_id) if self.ctx.bot_manager else None
                if bot is None:
                    continue
                remaining = max(order.amount - (order.paid_total or 0), 0)
                for admin_id in items_owner[1]:
                    lang = await get_lang(self.ctx.redis, admin_id)
                    try:
                        await bot.send_message(admin_id, t(
                            "cancelled_timeout", lang, id=order.id, remaining=remaining,
                            paid=order.paid_total or 0, cur=order.currency,
                            name=order.customer_name or order.customer_id,
                            phone=order.customer_phone or "—"))
                    except Exception:
                        pass
                lang = await get_lang(self.ctx.redis, order.customer_id)
                try:
                    await bot.send_message(order.customer_id, t(
                        "cancelled_customer", lang, id=order.id,
                        paid=order.paid_total or 0, cur=order.currency))
                except Exception:
                    pass

    def start_all(self):
        loops = [
            ("expirations", self._loop(self.check_expirations, EXPIRATION_INTERVAL)),
            ("reminders", self._loop(self.send_reminders, REMINDER_INTERVAL)),
            ("affpay_monitor", self._loop(self.monitor_affiliate_payments, AFFPAY_MONITOR_INTERVAL)),
            ("consistency", self._loop(self.consistency_check, CONSISTENCY_INTERVAL)),
            ("topup_sweep", self._loop(self.sweep_topups, TOPUP_SWEEP_INTERVAL)),
        ]
        for name, coro in loops:
            self._tasks.append(asyncio.create_task(coro, name=f"bg:{name}"))
        logger.info("[BG] %s background tasks started", len(self._tasks))

    async def stop_all(self):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _loop(self, fn, interval: int):
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[BG] task %s failed — continuing", fn.__name__)
            await asyncio.sleep(interval)

    # ----------------------------------------------------------- expirations

    async def check_expirations(self):
        ctx = self.ctx
        async with ctx.db() as session:
            expired = await ctx.shops.get_expired_shops(session)
            for shop in expired:
                logger.info("[BG] subscription expired for shop %s (%s)", shop.shop_id, shop.name)
                await ctx.shops.set_subscription_active(session, shop.shop_id, 0)
                await ctx.bot_manager.stop_shop(shop.shop_id)
                try:
                    await ctx.master_bot.send_message(
                        shop.admin_id,
                        f"⛔ The subscription for <b>{shop.name}</b> has expired and the bot was stopped.\n"
                        "Renew to bring it back online.",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="💳 Renew", callback_data=f"subscribe_{shop.shop_id}")]
                        ]),
                    )
                except Exception:
                    logger.debug("[BG] could not notify owner %s", shop.admin_id)

    # ------------------------------------------------------------- reminders

    async def send_reminders(self):
        ctx = self.ctx
        async with ctx.db() as session:
            expiring = await ctx.shops.get_shops_expiring_within(session, REMINDER_WINDOW_DAYS)
            now = utcnow()
            for shop in expiring:
                if shop.last_reminder_sent and now - shop.last_reminder_sent < timedelta(hours=24):
                    continue
                days_left = max(0, (shop.subscription_end - now).days)
                try:
                    await ctx.master_bot.send_message(
                        shop.admin_id,
                        f"⏰ Heads up — the subscription for <b>{shop.name}</b> expires in "
                        f"<b>{days_left} day(s)</b> ({shop.subscription_end.date()}).",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="💳 Renew now", callback_data=f"subscribe_{shop.shop_id}")]
                        ]),
                    )
                    await ctx.shops.update_last_reminder_sent(session, shop.shop_id)
                except Exception:
                    logger.debug("[BG] reminder to %s failed", shop.admin_id)

    # -------------------------------------------- affiliate payment monitor

    async def monitor_affiliate_payments(self):
        """Pick up paid AffiliatePayments from shop bots, validate, burn the
        affiliate's commission credits, activate the shop, launch its bot."""
        ctx = self.ctx
        async with ctx.db() as session:
            payments = await ctx.payments.get_unprocessed_affiliate_payments(session)
            for payment in payments:
                if payment.retry_count >= MAX_AFFPAY_RETRIES:
                    continue

                ok, msg = await ctx.payments.validate_affiliate_payment(session, payment)
                if not ok:
                    payment.retry_count += 1
                    payment.last_retry_at = utcnow()
                    payment.error_message = msg[:500]
                    await session.commit()
                    if payment.retry_count >= MAX_AFFPAY_RETRIES:
                        await self._alert_admin(
                            f"🚨 Affiliate payment <code>{payment.invoice_id}</code> failed validation "
                            f"{MAX_AFFPAY_RETRIES} times:\n{msg}\n\nManual intervention needed."
                        )
                    continue

                activated = await ctx.payments.activate_shop_with_affiliate_payment(session, payment)
                if not activated:
                    payment.retry_count += 1
                    payment.last_retry_at = utcnow()
                    await session.commit()
                    continue

                shop = await ctx.shops.get_shop(session, payment.shop_id)
                if shop:
                    token = await ctx.cache.get_shop_token(shop.shop_id) or shop.token
                    if token:
                        started, info = await ctx.bot_manager.start_shop(shop.shop_id, token)
                        if not started:
                            logger.warning("[BG] shop %s activated but bot launch failed: %s", shop.shop_id, info)
                    try:
                        await ctx.master_bot.send_message(
                            shop.admin_id,
                            f"✅ Payment confirmed — <b>{shop.name}</b> is now active until "
                            f"{shop.subscription_end.date() if shop.subscription_end else 'N/A'}! 🎉",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                    try:
                        await ctx.master_bot.send_message(
                            payment.affiliate_id,
                            f"💰 A referred shop paid <b>{payment.amount} ⭐</b> through your bot — "
                            "the Stars are yours, and the shop has been activated. "
                            f"{ctx.affiliates.commission_credits} commission credits were used.",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass

    async def _alert_admin(self, text: str):
        if not self.ctx.config.super_admin_id:
            return
        try:
            await self.ctx.master_bot.send_message(self.ctx.config.super_admin_id, text, parse_mode="HTML")
        except Exception:
            logger.debug("[BG] could not alert super admin")

    # ----------------------------------------------------------- consistency

    async def consistency_check(self):
        """Active shops must be running; inactive shops must not be."""
        ctx = self.ctx
        async with ctx.db() as session:
            active = await ctx.shops.get_active_shops(session)
        active_ids = {s.shop_id for s in active}

        for shop in active:
            if not ctx.bot_manager.is_running(shop.shop_id):
                token = await ctx.cache.get_shop_token(shop.shop_id) or shop.token
                if not token:
                    logger.error("[BG] active shop %s has no token — cannot start", shop.shop_id)
                    continue
                logger.info("[BG] restarting bot for active shop %s", shop.shop_id)
                ok, info = await ctx.bot_manager.start_shop(shop.shop_id, token)
                if not ok:
                    logger.error("[BG] restart failed for shop %s: %s", shop.shop_id, info)

        for shop_id in ctx.bot_manager.running_shop_ids():
            if shop_id not in active_ids:
                logger.info("[BG] stopping bot for inactive shop %s", shop_id)
                await ctx.bot_manager.stop_shop(shop_id)
