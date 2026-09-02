"""Shop data operations. v2: purely database + cache — no script generation,
no subprocess launching, no PID bookkeeping (see core/bot_manager.py)."""
import logging
import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import (
    Affiliate, AffiliateCode, AffiliatePayment, Category, Order, Payment,
    PaymentAudit, Product, Shop, ShopAnalytics, ShopSettings, StoreAdmin,
)
from services.cache_service import CacheService

logger = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def shop_to_dict(shop: Shop) -> dict:
    return {
        "shop_id": shop.shop_id,
        "name": shop.name,
        "admin_id": shop.admin_id,
        "token": shop.token,
        "username": shop.username,
        "subscription_active": shop.subscription_active,
        "subscription_start": shop.subscription_start,
        "subscription_end": shop.subscription_end,
        "affiliate_id": shop.affiliate_id,
        "affiliate_shop_id": shop.affiliate_shop_id,
        "last_reminder_sent": shop.last_reminder_sent,
        "is_trial": shop.is_trial,
        "trial_used": shop.trial_used,
    }


class ShopService:
    def __init__(self, cache: CacheService):
        self.cache = cache

    # --------------------------------------------------------------- loading

    async def load_shop(self, session: AsyncSession, shop_id: str, force_reload: bool = False) -> dict | None:
        if not force_reload:
            cached = await self.cache.cache_get(f"shop:{shop_id}")
            if cached:
                # Re-hydrate datetimes lost in JSON round-trip
                for key in ("subscription_start", "subscription_end", "last_reminder_sent"):
                    if cached.get(key) and isinstance(cached[key], str):
                        try:
                            cached[key] = datetime.fromisoformat(cached[key])
                        except ValueError:
                            cached[key] = None
                return cached
        shop = await session.get(Shop, shop_id)
        if not shop:
            return None
        data = shop_to_dict(shop)
        await self.cache.cache_set(f"shop:{shop_id}", data)
        return data

    async def get_shop(self, session: AsyncSession, shop_id: str) -> Shop | None:
        return await session.get(Shop, shop_id)

    async def get_shops_by_admin(self, session: AsyncSession, admin_id: int) -> list[Shop]:
        result = await session.execute(select(Shop).where(Shop.admin_id == admin_id).order_by(Shop.name))
        return list(result.scalars().all())

    async def get_all_shops(self, session: AsyncSession) -> list[Shop]:
        result = await session.execute(select(Shop).order_by(Shop.name))
        return list(result.scalars().all())

    async def get_active_shops(self, session: AsyncSession) -> list[Shop]:
        result = await session.execute(select(Shop).where(Shop.subscription_active == 1))
        return list(result.scalars().all())

    # ------------------------------------------------------------ validation

    @staticmethod
    def validate_token_format(token: str) -> bool:
        return bool(token) and bool(TOKEN_RE.match(token.strip()))

    async def check_shop_name_exists(self, session: AsyncSession, admin_id: int, shop_name: str) -> bool:
        result = await session.execute(
            select(Shop.shop_id).where(Shop.admin_id == admin_id, Shop.name == shop_name)
        )
        return result.scalar_one_or_none() is not None

    async def check_token_already_used(self, session: AsyncSession, token: str, exclude_shop_id: str | None = None) -> bool:
        stmt = select(Shop.shop_id).where(Shop.token == token)
        if exclude_shop_id:
            stmt = stmt.where(Shop.shop_id != exclude_shop_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None

    # -------------------------------------------------------------- mutation

    async def create_shop(
        self,
        session: AsyncSession,
        *,
        shop_id: str,
        name: str,
        admin_id: int,
        token: str,
        username: str,
        affiliate_id: int | None = None,
        affiliate_shop_id: str | None = None,
        currency: str = "ETB",
    ) -> Shop:
        shop = Shop(
            shop_id=shop_id,
            name=name,
            admin_id=admin_id,
            token=token,
            username=username,
            subscription_active=0,
            affiliate_id=affiliate_id,
            affiliate_shop_id=affiliate_shop_id,
        )
        session.add(shop)
        session.add(ShopSettings(shop_id=shop_id, currency=currency))
        session.add(StoreAdmin(shop_id=shop_id, user_id=admin_id, admin_type="owner"))
        await session.commit()
        await self.cache.store_shop_token(shop_id, token)
        logger.info("[SHOP] created shop %s (%s) for admin %s", shop_id, name, admin_id)
        return shop

    async def start_trial(self, session: AsyncSession, shop_id: str, days: int) -> Shop | None:
        """Put a shop live on a free trial. One trial per shop, ever."""
        shop = await session.get(Shop, shop_id, with_for_update=True)
        if not shop or shop.trial_used:
            return None
        now = utcnow()
        shop.subscription_active = 1
        shop.subscription_start = now
        shop.subscription_end = now + timedelta(days=days)
        shop.is_trial = 1
        shop.trial_used = 1
        await session.commit()
        await self.cache.invalidate_shop(shop_id)
        logger.info("[SHOP] trial started for %s (%s days)", shop_id, days)
        return shop

    async def activate_subscription(self, session: AsyncSession, shop_id: str, days: int) -> Shop | None:
        shop = await session.get(Shop, shop_id, with_for_update=True)
        if not shop:
            return None
        now = utcnow()
        # Paying during a trial should extend from the trial's end, not truncate it.
        base = shop.subscription_end if (shop.subscription_end and shop.subscription_end > now) else now
        shop.subscription_active = 1
        shop.subscription_start = now
        shop.subscription_end = base + timedelta(days=days)
        shop.is_trial = 0
        await session.commit()
        await self.cache.invalidate_shop(shop_id)
        return shop

    async def set_subscription_active(self, session: AsyncSession, shop_id: str, active: int) -> bool:
        shop = await session.get(Shop, shop_id)
        if not shop:
            return False
        shop.subscription_active = active
        await session.commit()
        await self.cache.invalidate_shop(shop_id)
        return True

    async def update_last_reminder_sent(self, session: AsyncSession, shop_id: str):
        shop = await session.get(Shop, shop_id)
        if shop:
            shop.last_reminder_sent = utcnow()
            await session.commit()
            await self.cache.invalidate_shop(shop_id)

    async def delete_shop_completely(self, session: AsyncSession, shop_id: str) -> bool:
        """Delete a shop and every related row (mirrors v1 semantics)."""
        try:
            for model in (Order, PaymentAudit, AffiliatePayment, AffiliateCode, Affiliate,
                          Product, Category, StoreAdmin, ShopAnalytics, ShopSettings, Payment):
                col = getattr(model, "shop_id", None)
                if col is not None:
                    await session.execute(delete(model).where(col == shop_id))
            await session.execute(delete(Shop).where(Shop.shop_id == shop_id))
            await session.commit()
            await self.cache.invalidate_shop(shop_id)
            await self.cache.delete_shop_token(shop_id)
            logger.info("[SHOP] deleted shop %s completely", shop_id)
            return True
        except Exception:
            await session.rollback()
            logger.exception("[SHOP] failed to delete shop %s", shop_id)
            return False

    # ------------------------------------------------------------- lifecycle

    async def get_expired_shops(self, session: AsyncSession) -> list[Shop]:
        result = await session.execute(
            select(Shop).where(Shop.subscription_active == 1, Shop.subscription_end < utcnow())
        )
        return list(result.scalars().all())

    async def get_shops_expiring_within(self, session: AsyncSession, days: int) -> list[Shop]:
        now = utcnow()
        result = await session.execute(
            select(Shop).where(
                Shop.subscription_active == 1,
                Shop.subscription_end >= now,
                Shop.subscription_end <= now + timedelta(days=days),
            )
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------- analytics

    async def increment_analytics(self, session: AsyncSession, shop_id: str):
        try:
            today = date.today()
            row = await session.get(ShopAnalytics, (shop_id, today))
            if row:
                row.visits = (row.visits or 0) + 1
            else:
                session.add(ShopAnalytics(shop_id=shop_id, date=today, visits=1))
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.warning("increment_analytics(%s) failed: %s", shop_id, e)

    # ------------------------------------------------------------- affiliate

    async def get_shop_bot_username_by_affiliate(
        self, session: AsyncSession, affiliate_id: int, created_shop_id: str
    ) -> str | None:
        """Username of the shop bot that GENERATED the affiliate link for
        `created_shop_id` (the shop being paid for) — used to redirect the
        buyer to the affiliate's own bot for payment."""
        created = await session.get(Shop, created_shop_id)
        if not created or not created.affiliate_shop_id:
            return None
        source = await session.get(Shop, created.affiliate_shop_id)
        if not source or source.admin_id != affiliate_id or source.subscription_active != 1:
            return None
        return (source.username or "").lstrip("@") or None
