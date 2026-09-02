"""Teleshop v2 entry point — one process runs the master bot AND every shop bot.

Architecture:
    master Bot  ── master Dispatcher (master routers)
    shop Bot ×N ── ONE shared shop Dispatcher (shop routers)
                     └── ShopContextMiddleware maps bot.id -> shop_id
    BotManager  ── starts/stops shop polling tasks at runtime
    BackgroundTasks ── expirations, reminders, affiliate payment monitor,
                       consistency check
"""
import asyncio
import logging
import signal

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import Config
from context import AppContext
from core.bot_manager import BotManager
from core.logging_setup import setup_logging
from core.polling import poll_bot
from master.router import build_master_router
from services import (
    AffiliateService, CacheService, DatabaseService,
    OrderService, PaymentService, ShopService,
)
from services.background_tasks import BackgroundTasks
from shopbot.middleware import ShopContextMiddleware
from shopbot.router import build_shop_router
from web.app import build_web_app

logger = logging.getLogger(__name__)


async def main() -> None:
    config = Config.load()
    setup_logging(config.log_dir, config.log_level)
    logger.info("Teleshop v2 starting…")

    # ------------------------------------------------------- infrastructure
    engine = create_async_engine(config.database_url, echo=False, pool_size=20, max_overflow=10, pool_timeout=30)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    redis = Redis.from_url(config.redis_url, decode_responses=True)

    async with session_factory() as session:
        await session.execute(text("SELECT 1"))
    logger.info("✅ Database connection verified")
    await redis.ping()
    logger.info("✅ Redis connection verified")

    await DatabaseService(engine).initialize_database()

    # ---------------------------------------------------------------- bots
    master_bot = Bot(token=config.bot_token)
    me = await master_bot.get_me()
    await master_bot.delete_webhook(drop_pending_updates=False)

    # Two dispatchers, one Redis. Shop FSM keys MUST include bot_id because
    # many bots share one storage (DefaultKeyBuilder(with_bot_id=True)).
    master_dp = Dispatcher(storage=RedisStorage(redis, key_builder=DefaultKeyBuilder(prefix="fsm_master")))
    shop_dp = Dispatcher(storage=RedisStorage(redis, key_builder=DefaultKeyBuilder(prefix="fsm_shop", with_bot_id=True)))

    # ------------------------------------------------------------- services
    cache = CacheService(redis)
    shops = ShopService(cache)
    affiliates = AffiliateService(config.affiliate_commission_credits)
    payments = PaymentService(config, affiliates)
    orders = OrderService()

    ctx = AppContext(
        config=config, engine=engine, db=session_factory, redis=redis,
        master_bot=master_bot, cache=cache, shops=shops,
        payments=payments, affiliates=affiliates, orders=orders,
        master_bot_username=me.username,
    )

    bot_manager = BotManager(shop_dp)
    ctx.bot_manager = bot_manager

    async def on_token_invalid(shop_id: str):
        """A shop bot's token was revoked at runtime — deactivate + tell owner."""
        async with session_factory() as session:
            shop = await shops.get_shop(session, shop_id)
            if not shop:
                return
            await shops.set_subscription_active(session, shop_id, 0)
            try:
                await master_bot.send_message(
                    shop.admin_id,
                    f"⚠️ The bot token for <b>{shop.name}</b> is no longer valid "
                    "(revoked at @BotFather?). The shop was paused — update the token to resume.",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    bot_manager.on_token_invalid = on_token_invalid

    # --------------------------------------------------------------- routing
    master_dp["ctx"] = ctx
    shop_dp["ctx"] = ctx
    master_dp.include_router(build_master_router())
    shop_dp.include_router(build_shop_router())
    shop_dp.update.outer_middleware(ShopContextMiddleware(ctx))

    # --------------------------------------------------- resume active shops
    async with session_factory() as session:
        active_shops = await shops.get_active_shops(session)
    logger.info("Resuming %s active shop bot(s)…", len(active_shops))
    for shop in active_shops:
        token = await cache.get_shop_token(shop.shop_id) or shop.token
        if not token:
            logger.error("No token available for active shop %s — skipping", shop.shop_id)
            continue
        if not await cache.get_shop_token(shop.shop_id):
            await cache.store_shop_token(shop.shop_id, token)  # heal Redis copy from DB
        ok, info = await bot_manager.start_shop(shop.shop_id, token)
        if not ok:
            logger.error("Could not resume shop %s: %s", shop.shop_id, info)

    # ------------------------------------------------------ background tasks
    background = BackgroundTasks(ctx)
    background.start_all()

    # ------------------------------------------------------- web dashboard
    # Served from this same process: same engine, same session factory, same
    # models. No REST layer, no CORS, no second deployment to keep in sync.
    import uvicorn
    web_app = build_web_app(ctx)
    web_server = uvicorn.Server(uvicorn.Config(
        web_app, host=config.web_host, port=config.web_port,
        log_level=config.log_level.lower(), access_log=False,
    ))
    web_task = asyncio.create_task(web_server.serve(), name="web-dashboard")
    logger.info("🖥  Dashboard on http://%s:%s (public: %s)",
                config.web_host, config.web_port, config.web_base_url)

    # ------------------------------------------------------- run master bot
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # e.g. Windows

    master_task = asyncio.create_task(poll_bot(master_dp, master_bot, name="master"), name="master-polling")
    logger.info("🚀 Teleshop v2 is live as @%s", me.username)

    await stop_event.wait()
    logger.info("Shutting down…")

    master_task.cancel()
    try:
        await master_task
    except (asyncio.CancelledError, Exception):
        pass
    web_server.should_exit = True
    try:
        await asyncio.wait_for(web_task, timeout=10)
    except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
        web_task.cancel()
    await background.stop_all()
    await bot_manager.stop_all()
    await master_bot.session.close()
    await redis.aclose()
    await engine.dispose()
    logger.info("Goodbye 👋")


if __name__ == "__main__":
    asyncio.run(main())
