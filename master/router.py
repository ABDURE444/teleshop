"""Aggregates all master-bot routers. Registration order matters where
callback prefixes overlap (e.g. delete_confirm_ before delete_ — handled
inside shops.py by declaration order)."""
from aiogram import Router

from master.handlers import affiliate, shops, start, subscription


def build_master_router() -> Router:
    router = Router(name="master")
    router.include_router(start.router)
    router.include_router(shops.router)
    router.include_router(subscription.router)
    router.include_router(affiliate.router)
    return router
