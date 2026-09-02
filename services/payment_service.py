"""Subscription payment records, validation, and affiliate-payment activation.

Ported from v1 with the activation logic intact; hard-coded amounts are now
driven by Config, and shop-bot launching is delegated to the BotManager by
callers (this service never touches processes).
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import Config
from models import Affiliate, AffiliatePayment, Payment, PaymentAudit, Shop
from services.affiliate_service import AffiliateService

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PaymentService:
    def __init__(self, config: Config, affiliates: AffiliateService):
        self.config = config
        self.affiliates = affiliates

    # ---------------------------------------------------------------- basics

    async def create_payment(
        self, session: AsyncSession, invoice_id: str, shop_id: str,
        plan: str, amount: int, currency: str = "XTR",
    ) -> bool:
        try:
            session.add(Payment(
                invoice_id=invoice_id, shop_id=shop_id, plan=plan,
                amount=amount, currency=currency, status="pending", created_at=utcnow(),
            ))
            await session.commit()
            logger.info("[PAYMENT] created %s shop=%s amount=%s", invoice_id, shop_id, amount)
            return True
        except Exception:
            await session.rollback()
            logger.exception("[PAYMENT] create_payment failed for %s", invoice_id)
            return False

    async def get_payment(self, session: AsyncSession, invoice_id: str) -> Payment | None:
        try:
            return await session.get(Payment, invoice_id)
        except Exception:
            logger.exception("[PAYMENT] get_payment failed for %s", invoice_id)
            return None

    async def update_payment_status(self, session: AsyncSession, invoice_id: str, status: str) -> bool:
        try:
            payment = await session.get(Payment, invoice_id)
            if not payment:
                logger.error("[PAYMENT] %s not found for status update", invoice_id)
                return False
            payment.status = status
            payment.updated_at = utcnow()
            await session.commit()
            return True
        except Exception:
            await session.rollback()
            logger.exception("[PAYMENT] update_payment_status failed for %s", invoice_id)
            return False

    async def check_payment_validity(
        self, session: AsyncSession, invoice_id: str,
        expected_amount: int, expected_currency: str = "XTR",
    ):
        """Pre-checkout validation. Returns (ok, payment_or_error_string)."""
        payment = await self.get_payment(session, invoice_id)
        if not payment:
            return False, "Payment not found"
        if payment.status != "pending":
            return False, f"Payment already {payment.status}"
        amount = int(payment.amount) if payment.amount else 0
        if amount != expected_amount:
            return False, f"Amount mismatch: expected {expected_amount}, got {amount}"
        if payment.currency != expected_currency:
            return False, "Currency mismatch"
        if payment.created_at and (utcnow() - payment.created_at).total_seconds() > 3600:
            return False, "Payment expired"
        return True, payment

    async def get_master_earnings(self, session: AsyncSession) -> int:
        result = await session.execute(
            select(Payment.amount).where(Payment.status == "paid", Payment.plan == "yearly")
        )
        return int(sum(row[0] or 0 for row in result.fetchall()))

    # --------------------------------------------- affiliate shop activation

    async def get_unprocessed_affiliate_payments(self, session: AsyncSession, limit: int = 10):
        result = await session.execute(
            select(AffiliatePayment)
            .where(
                AffiliatePayment.status == "paid",
                AffiliatePayment.master_notified == False,  # noqa: E712
                AffiliatePayment.shop_activated == False,   # noqa: E712
            )
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def validate_affiliate_payment(
        self, session: AsyncSession, payment: AffiliatePayment
    ) -> tuple[bool, str]:
        errors: list[str] = []

        if payment.status != "paid":
            errors.append(f"status is {payment.status}, expected 'paid'")
        if payment.amount != self.config.affiliate_payment_amount:
            errors.append(f"invalid amount {payment.amount}, expected {self.config.affiliate_payment_amount}")
        if payment.currency != "XTR":
            errors.append(f"invalid currency {payment.currency}")
        if payment.master_notified:
            errors.append("already processed by master")
        if payment.created_at and utcnow() - payment.created_at > timedelta(hours=24):
            errors.append("payment expired (>24h)")

        shop = await session.get(Shop, payment.shop_id)
        if not shop:
            errors.append(f"shop {payment.shop_id} not found")
        else:
            if shop.affiliate_id != payment.affiliate_id:
                errors.append("affiliate mismatch on shop record")
            if shop.subscription_active == 1:
                errors.append("shop already activated")

        source_shop_id = await self.affiliates.get_source_shop_id(session, payment.affiliate_id, payment.shop_id)
        if not source_shop_id:
            errors.append("could not resolve shop that generated the affiliate link")
        else:
            source = await session.get(Shop, source_shop_id)
            if not source:
                errors.append(f"source shop {source_shop_id} missing")
            elif source.admin_id != payment.affiliate_id:
                errors.append("source shop does not belong to affiliate")
            else:
                balance = await self.affiliates.get_balance(session, payment.affiliate_id, source_shop_id)
                if balance < self.affiliates.commission_credits:
                    errors.append(f"insufficient affiliate credits: {balance} < {self.affiliates.commission_credits}")

        if errors:
            msg = "; ".join(errors)
            logger.warning("[PAYMENT] affiliate payment %s validation failed: %s", payment.invoice_id, msg)
            return False, msg
        return True, "OK"

    async def activate_shop_with_affiliate_payment(
        self, session: AsyncSession, payment: AffiliatePayment
    ) -> bool:
        """Atomically: deduct commission credits from the affiliate's SOURCE
        shop record, activate the referred shop, mark the payment processed,
        and write an audit row. The affiliate keeps the Stars the buyer sent
        to their bot; only the earned commission credits are burned."""
        try:
            source_shop_id = await self.affiliates.get_source_shop_id(session, payment.affiliate_id, payment.shop_id)
            if not source_shop_id:
                await session.rollback()
                logger.error("[PAYMENT] no source shop for affiliate payment %s", payment.invoice_id)
                return False

            credits = self.affiliates.commission_credits
            ok, previous, new_balance = await self.affiliates.deduct_credits(
                session, payment.affiliate_id, source_shop_id, credits
            )
            if not ok:
                await session.rollback()
                logger.error(
                    "[PAYMENT] insufficient credits for affiliate %s on %s (%s < %s)",
                    payment.affiliate_id, source_shop_id, previous, credits,
                )
                return False

            result = await session.execute(
                select(Shop).where(Shop.shop_id == payment.shop_id).with_for_update()
            )
            shop = result.scalar_one_or_none()
            if not shop:
                await session.rollback()
                return False
            if shop.subscription_active == 1:
                await session.rollback()
                logger.warning("[PAYMENT] shop %s already active, skipping", payment.shop_id)
                return False

            now = utcnow()
            shop.subscription_active = 1
            shop.subscription_start = now
            shop.subscription_end = now + timedelta(days=self.config.subscription_days)

            payment.master_notified = True
            payment.shop_activated = True
            payment.validated_at = now

            session.add(PaymentAudit(
                invoice_id=payment.invoice_id, action="shop_activated",
                shop_id=payment.shop_id, affiliate_id=payment.affiliate_id,
                amount=payment.amount, credits_deducted=credits,
                previous_balance=previous, new_balance=new_balance, timestamp=now,
            ))
            await session.commit()
            logger.info(
                "[PAYMENT] shop %s activated via affiliate payment %s; credits %s -> %s on %s",
                payment.shop_id, payment.invoice_id, previous, new_balance, source_shop_id,
            )
            return True
        except Exception:
            await session.rollback()
            logger.exception("[PAYMENT] activation transaction failed for %s", payment.invoice_id)
            return False

    async def log_payment_event(self, session: AsyncSession, invoice_id: str, action: str, **kwargs):
        try:
            session.add(PaymentAudit(invoice_id=invoice_id, action=action, timestamp=utcnow(), **kwargs))
            await session.flush()
            logger.info("[AUDIT] %s | invoice=%s | %s", action, invoice_id,
                        " | ".join(f"{k}={v}" for k, v in kwargs.items() if v is not None))
        except Exception:
            logger.exception("[AUDIT] failed to log event %s for %s", action, invoice_id)
