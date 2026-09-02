from datetime import datetime, timezone

from sqlalchemy import BigInteger, Column, Integer, String, Text, TIMESTAMP, ForeignKey, Index
from sqlalchemy.orm import relationship

from models.base import Base, BigIntPK


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Order(Base):
    """Customer order inside a shop bot (cart-based since v2.1).

    Payment is provider-agnostic: the shop shares payment instructions
    (CBE / Telebirr / bank account), the customer pays outside Telegram and
    submits a payment SCREENSHOT. Admins verify manually with one tap
    (Confirm / Payment short / Fake). Several payments may satisfy one
    order (top-up flow) — see OrderPayment.
    """
    __tablename__ = 'orders'

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    shop_id = Column(String, ForeignKey('shops.shop_id', ondelete='CASCADE'), nullable=False)
    # Legacy single-product columns (kept for v2.0 rows; cart orders leave them NULL)
    product_id = Column(BigInteger, ForeignKey('products.id', ondelete='SET NULL'), nullable=True)
    product_title = Column(Text)
    quantity = Column(Integer, default=1, nullable=False)

    customer_id = Column(BigInteger, nullable=False)
    customer_username = Column(Text)
    customer_name = Column(Text)                    # Telegram first/last name snapshot
    customer_phone = Column(Text)                   # from request_contact / typed
    pickup_time = Column(Text)                      # free text: "ASAP", "12:45", …
    pickup_code = Column(String(8))                 # 4-digit code issued on confirm

    amount = Column(Integer, nullable=False)        # order total, in shop currency
    paid_total = Column(Integer, default=0, nullable=False)  # admin-recorded received sum
    currency = Column(Text, default='ETB', nullable=False)
    # pending -> awaiting_verification -> awaiting_topup -> paid -> fulfilled | cancelled
    status = Column(Text, default='pending', nullable=False)
    payment_provider = Column(Text, nullable=True)  # 'manual' | 'stars' | future adapters
    payment_reference = Column(Text, nullable=True) # optional bank txn ref
    refund_owed = Column(Integer, default=0, nullable=False)  # money received on a cancelled order
    collected_at = Column(TIMESTAMP, nullable=True)           # set when the customer picks up
    created_at = Column(TIMESTAMP, default=_utcnow, nullable=False)
    updated_at = Column(TIMESTAMP, nullable=True)

    __table_args__ = (
        Index('idx_orders_shop_id', 'shop_id'),
        Index('idx_orders_customer_id', 'customer_id'),
        Index('idx_orders_status', 'status'),
        {'comment': 'Customer orders placed inside shop bots'},
    )

    shop = relationship('Shop', back_populates='orders')
    items = relationship('OrderItem', back_populates='order', cascade='all, delete-orphan')
    order_payments = relationship('OrderPayment', back_populates='order', cascade='all, delete-orphan')


class OrderItem(Base):
    """One cart line. Title/price are snapshots — they survive product edits."""
    __tablename__ = 'order_items'

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    order_id = Column(BigInteger, ForeignKey('orders.id', ondelete='CASCADE'), nullable=False)
    product_id = Column(BigInteger, ForeignKey('products.id', ondelete='SET NULL'), nullable=True)
    title = Column(Text, nullable=False)
    unit_price = Column(Integer, nullable=False)
    qty = Column(Integer, default=1, nullable=False)

    __table_args__ = (
        Index('idx_order_items_order_id', 'order_id'),
        {'comment': 'Cart lines (snapshotted) per order'},
    )

    order = relationship('Order', back_populates='items')


class OrderPayment(Base):
    """One payment screenshot against an order (top-ups make several)."""
    __tablename__ = 'order_payments'

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    order_id = Column(BigInteger, ForeignKey('orders.id', ondelete='CASCADE'), nullable=False)
    seq = Column(Integer, default=1, nullable=False)          # 1 = first payment, 2+ = top-ups
    screenshot_file_id = Column(Text, nullable=False)          # Telegram file_id (no bytes stored)
    amount_confirmed = Column(Integer, nullable=True)          # set by admin on "Payment short"
    created_at = Column(TIMESTAMP, default=_utcnow, nullable=False)

    __table_args__ = (
        Index('idx_order_payments_order_id', 'order_id'),
        {'comment': 'Payment screenshots per order'},
    )

    order = relationship('Order', back_populates='order_payments')
