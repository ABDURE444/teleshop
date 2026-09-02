"""Application context: one object carrying every shared dependency.

v1 passed ~15 keyword arguments into every handler class constructor. In v2
this single context is stored on each Dispatcher (`dp["ctx"] = ctx`), and any
handler that needs it simply declares a `ctx: AppContext` parameter — aiogram
injects it from workflow data automatically.
"""
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from aiogram import Bot
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from config import Config

if TYPE_CHECKING:
    from core.bot_manager import BotManager
    from services.affiliate_service import AffiliateService
    from services.cache_service import CacheService
    from services.order_service import OrderService
    from services.payment_service import PaymentService
    from services.shop_service import ShopService


@dataclass
class AppContext:
    config: Config
    engine: AsyncEngine
    db: async_sessionmaker                 # session factory: `async with ctx.db() as session:`
    redis: Redis
    master_bot: Bot

    cache: "CacheService"
    shops: "ShopService"
    payments: "PaymentService"
    affiliates: "AffiliateService"
    orders: "OrderService"

    bot_manager: Optional["BotManager"] = None
    master_bot_username: str | None = None
