"""Shop bot: affiliate direct payments.

A buyer whose shop was referred by this bot's owner arrives via the deep link
    /start pay_shop_<created_shop_id>_affiliate_<affiliate_id>
pays the subscription in Stars to THIS bot (the affiliate keeps the Stars),
and the master bot's background monitor then validates the AffiliatePayment
row, burns the affiliate's commission credits, and activates the shop.
"""
import logging
import time

from aiogram import Bot, F, Router, types
from aiogram.enums import ContentType
from aiogram.types import LabeledPrice, PreCheckoutQuery
from sqlalchemy import select

from context import AppContext
from models import AffiliatePayment
from services.payment_service import utcnow

logger = logging.getLogger(__name__)
router = Router(name="shopbot.affiliate_payment")

AFFPAY_PREFIX = "affpay_"


def parse_pay_payload(payload: str) -> tuple[str, int] | None:
    # pay_shop_<shop_id>_affiliate_<affiliate_id>
    if not payload.startswith("pay_shop_"):
        return None
    try:
        rest = payload[len("pay_shop_"):]
        shop_part, affiliate_part = rest.split("_affiliate_", 1)
        return shop_part, int(affiliate_part)
    except (ValueError, IndexError):
        return None


async def begin_affiliate_payment(message: types.Message, payload: str, ctx: AppContext, host_shop_id: str, bot: Bot):
    """Called from customer.start_with_payload when a pay_shop_ deep link opens this bot."""
    parsed = parse_pay_payload(payload)
    if not parsed:
        await message.answer("❌ Invalid payment link.")
        return
    target_shop_id, affiliate_id = parsed
    amount = ctx.config.affiliate_payment_amount

    async with ctx.db() as session:
        # Sanity: this bot's shop must belong to the affiliate and be the
        # shop that generated the referral link for the target shop.
        target = await ctx.shops.get_shop(session, target_shop_id)
        if not target:
            await message.answer("❌ The shop this payment is for no longer exists.")
            return
        if target.subscription_active == 1:
            await message.answer("✅ That shop is already active — nothing to pay.")
            return
        if target.affiliate_id != affiliate_id or target.affiliate_shop_id != host_shop_id:
            logger.warning(
                "[AFFPAY] link mismatch: target=%s affiliate=%s host=%s (record: aff=%s src=%s)",
                target_shop_id, affiliate_id, host_shop_id, target.affiliate_id, target.affiliate_shop_id,
            )
            await message.answer("❌ This payment link doesn't match this shop. Please use the link from the master bot.")
            return
        balance = await ctx.affiliates.get_balance(session, affiliate_id, host_shop_id)
        if balance < ctx.affiliates.commission_credits:
            await message.answer("❌ This partner can't take payments right now. Please pay via the master bot instead.")
            return

        invoice_id = f"{AFFPAY_PREFIX}{target_shop_id}_{int(time.time())}"
        session.add(AffiliatePayment(
            invoice_id=invoice_id, shop_id=target_shop_id, affiliate_id=affiliate_id,
            amount=amount, currency="XTR", status="pending", created_at=utcnow(),
        ))
        await session.commit()

    try:
        await bot.send_invoice(
            chat_id=message.chat.id,
            title="Teleshop Yearly Subscription",
            description=f"Activate shop '{target.name}' for one year – {amount} ⭐",
            payload=invoice_id,
            currency="XTR",
            prices=[LabeledPrice(label="Yearly Plan", amount=amount)],
        )
        logger.info("[AFFPAY] invoice %s sent (shop %s via affiliate %s)", invoice_id, target_shop_id, affiliate_id)
    except Exception:
        logger.exception("[AFFPAY] failed to send invoice %s", invoice_id)
        await message.answer("❌ Could not create the invoice. Please try again later.")


@router.pre_checkout_query()
async def affpay_pre_checkout(pre_checkout_query: PreCheckoutQuery, ctx: AppContext):
    invoice_id = pre_checkout_query.invoice_payload
    if not invoice_id.startswith(AFFPAY_PREFIX):
        await pre_checkout_query.answer(ok=False, error_message="Unknown invoice")
        return
    async with ctx.db() as session:
        result = await session.execute(
            select(AffiliatePayment).where(AffiliatePayment.invoice_id == invoice_id)
        )
        payment = result.scalar_one_or_none()
    if not payment:
        await pre_checkout_query.answer(ok=False, error_message="Payment not found")
        return
    if payment.status != "pending":
        await pre_checkout_query.answer(ok=False, error_message=f"Payment already {payment.status}")
        return
    if pre_checkout_query.total_amount != payment.amount or pre_checkout_query.currency != "XTR":
        await pre_checkout_query.answer(ok=False, error_message="Amount mismatch")
        return
    await pre_checkout_query.answer(ok=True)


@router.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def affpay_success(message: types.Message, ctx: AppContext, shop_id: str):
    sp = message.successful_payment
    invoice_id = sp.invoice_payload
    if not invoice_id.startswith(AFFPAY_PREFIX):
        logger.warning("[AFFPAY] unexpected successful payment payload %s in shop %s", invoice_id, shop_id)
        return
    async with ctx.db() as session:
        result = await session.execute(
            select(AffiliatePayment).where(AffiliatePayment.invoice_id == invoice_id).with_for_update()
        )
        payment = result.scalar_one_or_none()
        if not payment:
            logger.error("[AFFPAY] paid invoice %s not found!", invoice_id)
            await message.answer("❌ Payment record not found — please contact support with this ID: " + invoice_id)
            return
        payment.status = "paid"
        payment.telegram_charge_id = sp.telegram_payment_charge_id
        payment.paid_at = utcnow()
        await session.commit()
    logger.info("[AFFPAY] invoice %s PAID — master monitor will activate shop %s", invoice_id, payment.shop_id)
    await message.answer(
        "✅ Payment received! Your shop is being activated — you'll get a "
        "confirmation from the master bot within a minute."
    )
