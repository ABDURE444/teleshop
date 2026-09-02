"""Aggregates all shop-bot routers (shared by every shop bot instance)."""
from aiogram import Router

from shopbot.handlers import admin, affiliate_payment, customer_v21, orders_admin, owner


def build_shop_router() -> Router:
    router = Router(name="shopbot")
    router.include_router(affiliate_payment.router)  # pre-checkout & payments first
    router.include_router(admin.router)
    router.include_router(owner.router)          # /today /collect /stock /dashboard
    router.include_router(orders_admin.router)       # order cards: Confirm/Short/Fake
    router.include_router(customer_v21.router)       # cart + screenshot checkout
    return router
