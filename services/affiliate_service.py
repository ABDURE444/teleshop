"""Affiliate program logic.

Semantics preserved exactly from v1 — the crucial invariant:

  * An affiliate earns `commission_credits` (default 300) on the affiliate
    record tied to the shop that GENERATED the referral link (their own
    shop), when a shop referred through that link pays its subscription.
  * When a buyer pays a referred shop's subscription THROUGH the affiliate's
    shop bot (direct affiliate payment), the commission credits — not the
    payment amount — are deducted from that same record, because the
    affiliate keeps the real Stars the buyer sent them.
"""
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Affiliate, Payment, Shop

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AffiliateService:
    def __init__(self, commission_credits: int):
        self.commission_credits = commission_credits

    # ------------------------------------------------------------- referrals

    @staticmethod
    def build_referral_payload(affiliate_id: int, source_shop_id: str) -> str:
        """Deep-link payload format kept compatible with v1 links in the wild."""
        return f"affiliate_{affiliate_id}_shop_{source_shop_id}"

    @staticmethod
    def parse_referral_payload(payload: str) -> tuple[int, str] | None:
        # affiliate_<user_id>_shop_<shop_id>
        if not payload.startswith("affiliate_"):
            return None
        try:
            rest = payload[len("affiliate_"):]
            affiliate_part, shop_part = rest.split("_shop_", 1)
            return int(affiliate_part), shop_part
        except (ValueError, IndexError):
            return None

    async def create_or_get_affiliate(self, session: AsyncSession, affiliate_id: int, shop_id: str) -> Affiliate:
        result = await session.execute(
            select(Affiliate).where(Affiliate.affiliate_id == affiliate_id, Affiliate.shop_id == shop_id)
        )
        affiliate = result.scalar_one_or_none()
        if affiliate:
            return affiliate
        affiliate = Affiliate(
            affiliate_id=affiliate_id, shop_id=shop_id,
            stars_earned=0, credit_balance=0, status="active", created_at=utcnow(),
        )
        session.add(affiliate)
        await session.commit()
        logger.info("[AFFILIATE] created affiliate record (%s, %s)", affiliate_id, shop_id)
        return affiliate

    # ------------------------------------------------------------ resolution

    async def get_source_shop_id(self, session: AsyncSession, affiliate_id: int, created_shop_id: str) -> str | None:
        """The shop that generated the affiliate link for `created_shop_id`."""
        created = await session.get(Shop, created_shop_id)
        if not created:
            return None
        if created.affiliate_id != affiliate_id:
            logger.warning(
                "[AFFILIATE] mismatch: shop %s has affiliate %s, asked about %s",
                created_shop_id, created.affiliate_id, affiliate_id,
            )
            return None
        return created.affiliate_shop_id

    # -------------------------------------------------------------- balances

    async def get_balance(self, session: AsyncSession, affiliate_id: int, source_shop_id: str) -> int:
        result = await session.execute(
            select(Affiliate.credit_balance).where(
                Affiliate.affiliate_id == affiliate_id, Affiliate.shop_id == source_shop_id
            )
        )
        return result.scalar_one_or_none() or 0

    async def get_balance_for_created_shop(
        self, session: AsyncSession, affiliate_id: int, created_shop_id: str
    ) -> int:
        """Credit balance on the shop that generated the link for `created_shop_id`."""
        source_shop_id = await self.get_source_shop_id(session, affiliate_id, created_shop_id)
        if not source_shop_id:
            return 0
        return await self.get_balance(session, affiliate_id, source_shop_id)

    async def get_total_stats(self, session: AsyncSession, affiliate_id: int) -> dict:
        result = await session.execute(select(Affiliate).where(Affiliate.affiliate_id == affiliate_id))
        rows = list(result.scalars().all())
        return {
            "total_credits": sum(r.credit_balance or 0 for r in rows),
            "total_stars_earned": sum(r.stars_earned or 0 for r in rows),
            "records": rows,
        }

    # ------------------------------------------------------------ commission

    async def process_commission(self, session: AsyncSession, created_shop_id: str) -> bool:
        """Credit the affiliate when a shop referred by them gets paid for.

        Credits land on the affiliate record of the SOURCE shop (the one that
        generated the link)."""
        try:
            created = await session.get(Shop, created_shop_id)
            if not created or not created.affiliate_id or not created.affiliate_shop_id:
                return False
            affiliate = await self.create_or_get_affiliate(session, created.affiliate_id, created.affiliate_shop_id)
            # Lock the row for a consistent increment
            result = await session.execute(
                select(Affiliate)
                .where(Affiliate.affiliate_id == affiliate.affiliate_id, Affiliate.shop_id == affiliate.shop_id)
                .with_for_update()
            )
            affiliate = result.scalar_one()
            affiliate.credit_balance = (affiliate.credit_balance or 0) + self.commission_credits
            affiliate.stars_earned = (affiliate.stars_earned or 0) + self.commission_credits
            await session.commit()
            logger.info(
                "[AFFILIATE] +%s credits to affiliate %s on shop %s (referred shop %s), balance=%s",
                self.commission_credits, affiliate.affiliate_id, affiliate.shop_id,
                created_shop_id, affiliate.credit_balance,
            )
            return True
        except Exception:
            await session.rollback()
            logger.exception("[AFFILIATE] process_commission failed for shop %s", created_shop_id)
            return False

    async def deduct_credits(
        self, session: AsyncSession, affiliate_id: int, source_shop_id: str, amount: int
    ) -> tuple[bool, int, int]:
        """Deduct credits; returns (ok, previous_balance, new_balance)."""
        result = await session.execute(
            select(Affiliate)
            .where(Affiliate.affiliate_id == affiliate_id, Affiliate.shop_id == source_shop_id)
            .with_for_update()
        )
        affiliate = result.scalar_one_or_none()
        if not affiliate:
            return False, 0, 0
        previous = affiliate.credit_balance or 0
        if previous < amount:
            return False, previous, previous
        affiliate.credit_balance = previous - amount
        # commit is left to the caller so it can be part of a larger transaction
        return True, previous, affiliate.credit_balance

    # ---------------------------------------------------------- payout queue

    async def create_payout_request(
        self, session: AsyncSession, affiliate_id: int, shop_id: str, amount: int
    ) -> str:
        """Record a pending manual payout (super admin sends Stars by hand)."""
        invoice_id = f"payout_{affiliate_id}_{int(time.time())}"
        session.add(Payment(
            invoice_id=invoice_id, shop_id=shop_id, plan="affiliate_payout",
            amount=amount, currency="XTR", status="pending", created_at=utcnow(),
        ))
        await session.commit()
        logger.info("[AFFILIATE] payout request %s created for affiliate %s", invoice_id, affiliate_id)
        return invoice_id
