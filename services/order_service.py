"""Orders placed by customers inside shop bots (new in v2)."""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Order, OrderItem, OrderPayment, Product, ShopSettings, StoreAdmin

logger = logging.getLogger(__name__)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class OrderService:
    async def create_order(
        self, session: AsyncSession, *, shop_id: str, product: Product,
        customer_id: int, customer_username: str | None, quantity: int = 1,
        currency: str = "ETB",
    ) -> Order:
        order = Order(
            shop_id=shop_id,
            product_id=product.id,
            product_title=product.title,
            customer_id=customer_id,
            customer_username=customer_username,
            quantity=quantity,
            amount=(product.price or 0) * quantity,
            currency=currency,
            status="pending",
            payment_provider="manual",
            created_at=utcnow(),
        )
        session.add(order)
        await session.commit()
        await session.refresh(order)
        logger.info("[ORDER] #%s created in shop %s by %s", order.id, shop_id, customer_id)
        return order


    # ------------------------------------------------------ cart orders (v2.1)

    async def create_cart_order(
        self, session: AsyncSession, *, shop_id: str, items: list[dict],
        customer_id: int, customer_username: str | None, customer_name: str | None,
        customer_phone: str | None, pickup_time: str | None, currency: str = "ETB",
    ) -> Order:
        """items: [{"product_id", "title", "price", "qty"}]"""
        total = sum(int(i["price"]) * int(i["qty"]) for i in items)
        order = Order(
            shop_id=shop_id, customer_id=customer_id,
            customer_username=customer_username, customer_name=customer_name,
            customer_phone=customer_phone, pickup_time=pickup_time,
            amount=total, currency=currency, status="pending",
            payment_provider="manual", created_at=utcnow(),
        )
        session.add(order)
        await session.flush()
        for i in items:
            session.add(OrderItem(
                order_id=order.id, product_id=i.get("product_id"),
                title=i["title"], unit_price=int(i["price"]), qty=int(i["qty"]),
            ))
        await session.commit()
        await session.refresh(order)
        logger.info("[ORDER] #%s (cart, %s items, %s %s) in shop %s by %s",
                    order.id, len(items), total, currency, shop_id, customer_id)
        return order

    async def get_items(self, session: AsyncSession, order_id: int) -> list[OrderItem]:
        result = await session.execute(
            select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.id)
        )
        return list(result.scalars().all())

    async def add_payment(self, session: AsyncSession, order_id: int,
                          screenshot_file_id: str) -> OrderPayment | None:
        order = await session.get(Order, order_id)
        if not order:
            return None
        result = await session.execute(
            select(OrderPayment).where(OrderPayment.order_id == order_id)
        )
        seq = len(result.scalars().all()) + 1
        payment = OrderPayment(order_id=order_id, seq=seq,
                               screenshot_file_id=screenshot_file_id, created_at=utcnow())
        session.add(payment)
        order.status = "awaiting_verification"
        order.updated_at = utcnow()
        await session.commit()
        await session.refresh(payment)
        return payment

    async def record_received(self, session: AsyncSession, order_id: int,
                              amount: int) -> Order | None:
        """Admin recorded an actually-received amount (Payment short flow)."""
        order = await session.get(Order, order_id)
        if not order:
            return None
        order.paid_total = (order.paid_total or 0) + amount
        remaining = order.amount - order.paid_total
        order.status = "paid" if remaining <= 0 else "awaiting_topup"
        order.updated_at = utcnow()
        await session.commit()
        await session.refresh(order)
        return order

    async def latest_open_order(self, session: AsyncSession, shop_id: str,
                                customer_id: int) -> Order | None:
        """The customer's newest order still waiting for money/screenshot."""
        result = await session.execute(
            select(Order).where(
                Order.shop_id == shop_id,
                Order.customer_id == customer_id,
                Order.status.in_(("pending", "awaiting_verification", "awaiting_topup")),
            ).order_by(Order.created_at.desc()).limit(1)
        )
        return result.scalars().first()

    async def get_order(self, session: AsyncSession, order_id: int) -> Order | None:
        return await session.get(Order, order_id)

    async def set_status(self, session: AsyncSession, order_id: int, status: str,
                         payment_reference: str | None = None) -> Order | None:
        order = await session.get(Order, order_id)
        if not order:
            return None
        order.status = status
        if payment_reference:
            order.payment_reference = payment_reference
        order.updated_at = utcnow()
        await session.commit()
        return order

    async def list_orders_for_shop(self, session: AsyncSession, shop_id: str, limit: int = 20) -> list[Order]:
        result = await session.execute(
            select(Order).where(Order.shop_id == shop_id).order_by(Order.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_shop_currency_and_instructions(session: AsyncSession, shop_id: str) -> tuple[str, str | None]:
        """Currency plus a rendered payment block.

        Structured fields (bank / account name / number) are rendered first so
        the customer always sees WHOSE account they are paying into before they
        send money; free-text notes are appended underneath.
        """
        settings = await session.get(ShopSettings, shop_id)
        if not settings:
            return "ETB", None
        currency = settings.currency or "ETB"
        lines = []
        if settings.account_number:
            if settings.bank_name:
                lines.append(f"🏦 {settings.bank_name}")
            lines.append(f"#️⃣ {settings.account_number}")
            if settings.account_name:
                lines.append(f"👤 {settings.account_name}")
        if settings.pickup_address:
            lines.append(f"📍 {settings.pickup_address}")
        if settings.payment_instructions:
            if lines:
                lines.append("")
            lines.append(settings.payment_instructions)
        return currency, ("\n".join(lines) if lines else None)

    @staticmethod
    async def get_settings(session: AsyncSession, shop_id: str) -> ShopSettings | None:
        return await session.get(ShopSettings, shop_id)

    async def sweep_abandoned_topups(self, session: AsyncSession) -> list[Order]:
        """Cancel part-paid orders the customer never completed.

        Money already received is recorded as `refund_owed` — the platform
        never holds funds, so all it can do is make the debt visible.
        """
        now = utcnow()
        result = await session.execute(
            select(Order).where(Order.status == "awaiting_topup")
        )
        cancelled: list[Order] = []
        for order in result.scalars().all():
            settings = await session.get(ShopSettings, order.shop_id)
            timeout = (settings.topup_timeout_minutes if settings else 60) or 60
            stale_since = order.updated_at or order.created_at
            if stale_since and now - stale_since >= timedelta(minutes=timeout):
                order.status = "cancelled"
                order.refund_owed = order.paid_total or 0
                order.updated_at = now
                cancelled.append(order)
        if cancelled:
            await session.commit()
            logger.info("[ORDER] swept %s abandoned top-up order(s)", len(cancelled))
        return cancelled

    async def today_orders(self, session: AsyncSession, shop_id: str) -> list[Order]:
        start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        result = await session.execute(
            select(Order).where(Order.shop_id == shop_id, Order.created_at >= start)
            .order_by(Order.created_at.desc())
        )
        return list(result.scalars().all())

    async def by_pickup_code(self, session: AsyncSession, shop_id: str, code: str) -> Order | None:
        result = await session.execute(
            select(Order).where(Order.shop_id == shop_id, Order.pickup_code == code,
                                Order.status == "paid").limit(1)
        )
        return result.scalars().first()

    async def mark_collected(self, session: AsyncSession, order_id: int) -> Order | None:
        order = await session.get(Order, order_id)
        if not order or order.status != "paid":
            return None
        order.status = "fulfilled"
        order.collected_at = utcnow()
        order.updated_at = utcnow()
        await session.commit()
        return order

    @staticmethod
    async def get_admin_ids(session: AsyncSession, shop_id: str) -> list[int]:
        result = await session.execute(select(StoreAdmin.user_id).where(StoreAdmin.shop_id == shop_id))
        return [row[0] for row in result.fetchall()]
