"""Master bot: subscription payments (Telegram Stars), affiliate redirect,
successful-payment activation with emergency recovery, and manual payout
confirmation. Ported from v1 payment_handler with the same guarantees:

  * a paid user's shop is ALWAYS activated, even if bot launch fails;
  * an unexpected error after payment triggers an emergency recovery path.
"""
import logging
import time

from aiogram import F, Router, types
from aiogram.enums import ContentType
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, PreCheckoutQuery
from sqlalchemy import select

from context import AppContext
from models import Payment
from payments import StarsProvider
from services.payment_service import utcnow

logger = logging.getLogger(__name__)
router = Router(name="master.subscription")

stars = StarsProvider()


# ------------------------------------------------------------- subscribe CTA

@router.callback_query(F.data.startswith("subscribe_"))
async def subscribe(query: types.CallbackQuery, ctx: AppContext):
    """Show the payment option — or redirect to the affiliate's shop bot when
    the affiliate has enough credits to take the payment directly."""
    await query.answer()
    shop_id = query.data[len("subscribe_"):]
    if not shop_id:
        await query.message.answer("Please select a shop first.")
        return

    async with ctx.db() as session:
        shop = await ctx.shops.load_shop(session, shop_id)
        if not shop:
            await query.message.answer("❌ Error: shop not found.")
            return

        if shop.get("subscription_active") == 1:
            await query.message.answer("✅ This shop is already active!")
            return

        # Affiliate redirect: buyer pays the affiliate's own shop bot.
        if shop.get("affiliate_id"):
            affiliate_id = shop["affiliate_id"]
            try:
                balance = await ctx.affiliates.get_balance_for_created_shop(session, affiliate_id, shop_id)
                logger.info("[SUBSCRIBE] shop %s referred by %s (balance %s)", shop_id, affiliate_id, balance)
                if balance >= ctx.config.affiliate_payout_threshold:
                    username = await ctx.shops.get_shop_bot_username_by_affiliate(session, affiliate_id, shop_id)
                    if username:
                        link = f"https://t.me/{username}?start=pay_shop_{shop_id}_affiliate_{affiliate_id}"
                        await query.message.answer(
                            "💳 <b>Payment via partner</b>\n\n"
                            "You'll be redirected to the partner's bot to pay.\n"
                            f"Amount: {ctx.config.affiliate_payment_amount} ⭐",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="💳 Pay now", url=link)]
                            ]),
                        )
                        return
                    logger.warning("[SUBSCRIBE] affiliate %s eligible but shop bot unavailable — falling back", affiliate_id)
            except Exception:
                logger.exception("[SUBSCRIBE] affiliate check failed for shop %s — falling back to direct payment", shop_id)

    price = ctx.config.subscription_price_stars
    await query.message.answer(
        "Choose a subscription plan:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🗓 Yearly – {price} ⭐", callback_data=f"pay_yearly_{shop_id}")]
        ]),
    )


@router.callback_query(F.data.startswith("pay_yearly_"))
async def pay_yearly(query: types.CallbackQuery, ctx: AppContext):
    await query.answer()
    shop_id = query.data[len("pay_yearly_"):]
    async with ctx.db() as session:
        shop = await ctx.shops.load_shop(session, shop_id)
    if not shop:
        await query.message.answer("❌ Error: shop not found.")
        return

    user_id = query.from_user.id
    invoice_id = f"{shop_id}_{user_id}_{int(time.time())}"
    price = ctx.config.subscription_price_stars

    async with ctx.db() as session:
        await ctx.payments.create_payment(session, invoice_id, shop_id, "yearly", price, "XTR")

    try:
        await stars.request_payment(
            ctx.master_bot, user_id,
            invoice_id=invoice_id, amount=price,
            title="Teleshop Yearly Subscription",
            description=f"Activate your shop for one year – {price} ⭐",
        )
    except Exception:
        logger.exception("[SUBSCRIBE] failed to send invoice %s", invoice_id)
        await query.message.answer("❌ Could not create invoice. Please try again later.")


# --------------------------------------------------------------- pre-checkout

@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery, ctx: AppContext):
    invoice_id = pre_checkout_query.invoice_payload
    async with ctx.db() as session:
        ok, result = await ctx.payments.check_payment_validity(
            session, invoice_id, ctx.config.subscription_price_stars, "XTR"
        )
    if ok:
        await pre_checkout_query.answer(ok=True)
    else:
        logger.error("[PAYMENT] pre-checkout rejected for %s: %s", invoice_id, result)
        await pre_checkout_query.answer(ok=False, error_message=result if isinstance(result, str) else "Payment validation failed")


# ---------------------------------------------------------- successful payment

@router.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def process_successful_payment(message: types.Message, ctx: AppContext):
    sp = message.successful_payment
    invoice_id = sp.invoice_payload
    shop_id = None
    logger.info("[PAYMENT] successful payment: invoice=%s amount=%s %s", invoice_id, sp.total_amount, sp.currency)

    try:
        if sp.currency != "XTR":
            await message.answer("❌ Only Telegram Stars (XTR) are accepted.")
            return
        if sp.total_amount != ctx.config.subscription_price_stars:
            logger.error("[PAYMENT] amount mismatch: got %s expected %s", sp.total_amount, ctx.config.subscription_price_stars)
            await message.answer("❌ Unexpected payment amount — please contact support.")
            return

        async with ctx.db() as session:
            await ctx.payments.update_payment_status(session, invoice_id, "paid")
            payment = await ctx.payments.get_payment(session, invoice_id)
            if not payment:
                await message.answer("❌ Error: payment not found. Please contact support.")
                return
            shop_id = payment.shop_id
            shop = await ctx.shops.get_shop(session, shop_id)
            if not shop:
                await message.answer("❌ Error: shop not found. Please contact support.")
                return
            if shop.subscription_active == 1:
                await message.reply("✅ Shop is already active! No action needed.")
                return

            # Affiliate commission (non-critical: never block activation)
            if shop.affiliate_id:
                try:
                    await ctx.affiliates.process_commission(session, shop_id)
                    balance = await ctx.affiliates.get_balance(
                        session, shop.affiliate_id, shop.affiliate_shop_id
                    ) if shop.affiliate_shop_id else 0
                    await ctx.master_bot.send_message(
                        shop.affiliate_id,
                        "🎉 <b>Referral commission earned!</b>\n\n"
                        f"A shop you referred just subscribed.\n"
                        f"➕ {ctx.config.affiliate_commission_credits} credits\n"
                        f"📊 Balance: {balance} credits\n\n"
                        f"At {ctx.config.affiliate_payout_threshold}+ credits, buyers you refer "
                        "pay through YOUR shop bot and you keep the Stars directly.",
                        parse_mode="HTML",
                    )
                except Exception:
                    logger.warning("[PAYMENT] affiliate commission failed (non-critical)", exc_info=True)

        # Activate (CRITICAL — the user has paid)
        async with ctx.db() as session:
            shop = await ctx.shops.activate_subscription(session, shop_id, ctx.config.subscription_days)

        # Launch the bot (non-critical: shop stays active even if this fails)
        bot_username = None
        try:
            token = await ctx.cache.get_shop_token(shop_id) or (shop.token if shop else None)
            if token:
                ok, info = await ctx.bot_manager.start_shop(shop_id, token)
                bot_username = info if ok else None
                if not ok:
                    logger.warning("[PAYMENT] shop %s ACTIVATED but bot launch failed: %s", shop_id, info)
        except Exception:
            logger.warning("[PAYMENT] shop %s ACTIVATED but bot launch raised", shop_id, exc_info=True)

        username = bot_username or ((shop.username or "").lstrip("@") if shop else None)
        keyboard = None
        if username:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛍 Go to Shop", url=f"https://t.me/{username}")]
            ])
        end = shop.subscription_end.date() if shop and shop.subscription_end else "N/A"
        await message.reply(
            f"✅ Yearly subscription activated! Expires: {end}\n\nYour shop is now live! 🎉",
            reply_markup=keyboard,
        )
        logger.info("[PAYMENT] payment processed successfully for shop %s", shop_id)

    except Exception:
        logger.exception("[PAYMENT] ERROR in payment processing for %s", invoice_id)
        # Emergency recovery: if the payment is marked paid, force-activate.
        if invoice_id and shop_id:
            try:
                async with ctx.db() as session:
                    payment = await ctx.payments.get_payment(session, invoice_id)
                    if payment and payment.status == "paid":
                        shop = await ctx.shops.get_shop(session, shop_id)
                        if shop and shop.subscription_active != 1:
                            logger.warning("[PAYMENT] emergency activation for shop %s", shop_id)
                            await ctx.shops.activate_subscription(session, shop_id, ctx.config.subscription_days)
                            await message.answer("✅ Payment processed — your shop is now active! 🎉")
                            return
            except Exception:
                logger.exception("[PAYMENT] emergency recovery failed for %s", invoice_id)
        await message.answer(
            "❌ <b>Error processing payment</b>\n\n"
            "If you were charged, contact support with:\n"
            f"• Invoice ID: <code>{invoice_id}</code>\n"
            f"• Shop ID: <code>{shop_id}</code>",
            parse_mode="HTML",
        )


# --------------------------------------------------- manual payout confirmation

@router.callback_query(F.data.startswith("mark_paid_"))
async def mark_payout_paid(query: types.CallbackQuery, ctx: AppContext):
    """Super admin confirms they manually sent Stars to an affiliate."""
    await query.answer()
    if query.from_user.id != ctx.config.super_admin_id:
        await query.message.answer("🚫 Admin access only")
        return

    short_invoice = query.data[len("mark_paid_"):]
    try:
        async with ctx.db() as session:
            result = await session.execute(
                select(Payment).where(
                    Payment.invoice_id.like(f"{short_invoice}%"),
                    Payment.status == "pending",
                    Payment.plan == "affiliate_payout",
                )
            )
            payment = result.scalar_one_or_none()
            if not payment:
                await query.message.answer(f"❌ No pending payout found for {short_invoice}")
                return

            shop = await ctx.shops.get_shop(session, payment.shop_id)
            if not shop or not shop.affiliate_id or not shop.affiliate_shop_id:
                await query.message.answer("❌ No affiliate found for that payout.")
                return

            affiliate_id = shop.affiliate_id
            amount = int(payment.amount or 0)
            ok, previous, new_balance = await ctx.affiliates.deduct_credits(
                session, affiliate_id, shop.affiliate_shop_id, amount
            )
            if not ok:
                await session.rollback()
                await query.message.answer("❌ Balance deduction failed — nothing changed.")
                return
            payment.status = "paid"
            payment.updated_at = utcnow()
            await session.commit()

        try:
            await ctx.master_bot.send_message(
                affiliate_id,
                "✅ <b>Payout confirmed!</b>\n\n"
                f"💰 You received <b>{amount} Telegram Stars</b>.\n"
                f"📊 Credits: {previous} → {new_balance}",
                parse_mode="HTML",
            )
        except Exception:
            logger.warning("[PAYOUT] could not notify affiliate %s", affiliate_id)

        await query.message.edit_text(
            f"✅ Payout confirmed.\n👤 Affiliate: {affiliate_id}\n💰 {amount} ⭐\n"
            f"📊 Credits: {previous} → {new_balance}",
        )
    except Exception as e:
        logger.exception("[PAYOUT] mark_paid failed")
        await query.message.answer(f"❌ Error: {e}")
