from sqlalchemy import (
    BigInteger, Column, Integer, String, Text, ForeignKey, Date, TIMESTAMP,
    Index, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from models.base import Base, BigIntPK


class Shop(Base):
    """A merchant shop. One shop == one dedicated Telegram bot token.

    v2 change: the `pid` column is gone — shop bots run as asyncio tasks
    inside the main process (see core/bot_manager.py), not OS subprocesses.
    """
    __tablename__ = 'shops'

    shop_id = Column(String, primary_key=True)
    name = Column(Text)
    admin_id = Column(BigInteger)                     # owner's Telegram user id
    token = Column(Text)                              # bot token (also mirrored in Redis, integrity-hashed)
    username = Column(Text)                           # shop bot @username
    subscription_active = Column(Integer, default=0)
    subscription_start = Column(TIMESTAMP)
    subscription_end = Column(TIMESTAMP)
    last_admin_index = Column(Integer, default=0)
    affiliate_id = Column(BigInteger, nullable=True)  # who referred this shop
    affiliate_shop_id = Column(String, ForeignKey('shops.shop_id', ondelete='SET NULL'), nullable=True)
    last_reminder_sent = Column(TIMESTAMP, nullable=True)
    is_trial = Column(Integer, default=0, nullable=False)   # 1 while on the free trial
    trial_used = Column(Integer, default=0, nullable=False) # 1 once a trial has been consumed

    __table_args__ = (
        UniqueConstraint('admin_id', 'name', name='unique_shop_name_per_admin'),
        UniqueConstraint('token', name='unique_shop_token'),
        Index('idx_shops_admin_name', 'admin_id', 'name'),
        Index('idx_shops_affiliate_shop_id', 'affiliate_shop_id'),
        {'comment': 'Shops table'},
    )

    categories = relationship('Category', back_populates='shop', cascade='all, delete-orphan')
    admins = relationship('StoreAdmin', back_populates='shop', cascade='all, delete-orphan')
    payments = relationship('Payment', back_populates='shop', cascade='all, delete-orphan')
    analytics = relationship('ShopAnalytics', back_populates='shop', cascade='all, delete-orphan')
    settings = relationship('ShopSettings', back_populates='shop', uselist=False, cascade='all, delete-orphan')
    products = relationship('Product', back_populates='shop')
    orders = relationship('Order', back_populates='shop', cascade='all, delete-orphan')


class StoreAdmin(Base):
    __tablename__ = 'store_admins'

    shop_id = Column(String, ForeignKey('shops.shop_id', ondelete='CASCADE'), primary_key=True)
    user_id = Column(BigInteger, primary_key=True)
    username = Column(Text)
    admin_type = Column(Text, default='shop_admin')   # 'owner' | 'shop_admin'

    __table_args__ = (
        Index('idx_store_admins_shop_id', 'shop_id'),
        {'comment': 'Store admins table'},
    )

    shop = relationship('Shop', back_populates='admins')


class Category(Base):
    __tablename__ = 'categories'

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    shop_id = Column(String, ForeignKey('shops.shop_id', ondelete='CASCADE'))
    name = Column(Text)

    __table_args__ = (
        UniqueConstraint('shop_id', 'name', name='unique_category_name_per_shop'),
        Index('idx_categories_shop_id', 'shop_id'),
        {'comment': 'Categories table'},
    )

    shop = relationship('Shop', back_populates='categories')
    products = relationship('Product', back_populates='category')


class Product(Base):
    __tablename__ = 'products'

    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    shop_id = Column(String, ForeignKey('shops.shop_id', ondelete='CASCADE'))
    category_id = Column(BigInteger, ForeignKey('categories.id'))
    title = Column(Text)
    description = Column(Text)
    price = Column(Integer)
    media_file_ids = Column(Text)   # JSON list of Telegram file_ids
    links = Column(Text)            # comma-separated external links
    available = Column(Integer, default=1, nullable=False)  # 0 = hidden from customers (out of stock)

    __table_args__ = (
        UniqueConstraint('category_id', 'title', name='unique_product_name_per_category'),
        Index('idx_products_shop_id', 'shop_id'),
        Index('idx_products_category_id', 'category_id'),
        {'comment': 'Products table'},
    )

    shop = relationship('Shop', back_populates='products')
    category = relationship('Category', back_populates='products')


class ShopSettings(Base):
    __tablename__ = 'shop_settings'

    shop_id = Column(String, ForeignKey('shops.shop_id', ondelete='CASCADE'), primary_key=True)
    currency = Column(Text, default='ETB')
    timezone = Column(Text, default='Africa/Addis_Ababa')
    # Free-text payment instructions shown to customers when they place an
    # order (e.g. CBE account 1000..., Telebirr 09..., Dashen ...).
    payment_instructions = Column(Text, nullable=True)
    # Structured payment details (v2.1). When set, these are rendered into the
    # customer's payment message; `payment_instructions` remains as free-text
    # extra notes. account_name is shown separately so the customer can see
    # exactly whose account they are paying into.
    bank_name = Column(Text, nullable=True)
    account_name = Column(Text, nullable=True)
    account_number = Column(Text, nullable=True)
    pickup_address = Column(Text, nullable=True)
    topup_timeout_minutes = Column(Integer, default=60, nullable=False)

    __table_args__ = ({'comment': 'Shop settings table'},)

    shop = relationship('Shop', back_populates='settings')


class ShopAnalytics(Base):
    __tablename__ = 'shop_analytics'

    shop_id = Column(String, ForeignKey('shops.shop_id', ondelete='CASCADE'), primary_key=True)
    date = Column(Date, primary_key=True)
    visits = Column(Integer, default=0)

    __table_args__ = ({'comment': 'Shop analytics table'},)

    shop = relationship('Shop', back_populates='analytics')
