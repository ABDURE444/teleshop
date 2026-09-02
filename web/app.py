"""Owner dashboard — FastAPI + Jinja2, served from the same process as the bots.

Same database, same models, same session factory as the bot: no REST layer,
no CORS, no second deploy. Product photos are pushed through the Bot API and
stored as Telegram file_ids, so Telegram serves every image for free and this
server stores no bytes.
"""
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram.types import BufferedInputFile
from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from core.i18n import LANG_NAMES, SUPPORTED, t
from models import (
    Category, Order, OrderItem, Product, Shop, ShopSettings, StoreAdmin,
)
from web.auth import COOKIE_NAME, current_user, make_session, verify_telegram_login

logger = logging.getLogger(__name__)

_BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_BASE / "templates"))

MAX_IMAGE_BYTES = 8 * 1024 * 1024


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def build_web_app(ctx) -> FastAPI:
    app = FastAPI(title="Teleshop dashboard", docs_url=None, redoc_url=None)
    app.state.ctx = ctx
    app.mount("/static", StaticFiles(directory=str(_BASE / "static")), name="static")

    # ------------------------------------------------------------- helpers

    def render(request: Request, template: str, **context):
        user = current_user(request)
        return templates.TemplateResponse(
            request, template,
            {"user": user, "lang_names": LANG_NAMES, "langs": SUPPORTED,
             "base_url": ctx.config.web_base_url, **context},
        )

    def need_login():
        return RedirectResponse("/login", status_code=303)

    async def owned_shop(session, shop_id: str, uid: int) -> Shop | None:
        """A shop the user owns or co-administers — everything else is 404."""
        shop = await session.get(Shop, shop_id)
        if not shop:
            return None
        if shop.admin_id == uid:
            return shop
        row = await session.execute(
            select(StoreAdmin).where(StoreAdmin.shop_id == shop_id,
                                     StoreAdmin.user_id == uid)
        )
        return shop if row.scalars().first() else None

    async def get_settings(session, shop_id: str) -> ShopSettings:
        st = await session.get(ShopSettings, shop_id)
        if not st:
            st = ShopSettings(shop_id=shop_id, currency=ctx.config.default_currency)
            session.add(st)
            await session.commit()
            await session.refresh(st)
        return st

    async def upload_to_telegram(shop: Shop, files: list[UploadFile]) -> list[str]:
        """Push images through the Bot API; keep only the returned file_ids.

        Telegram then hosts and serves every product photo at no cost — this
        server never stores image bytes and never pays egress for them.
        """
        file_ids: list[str] = []
        if not files:
            return file_ids
        bot = ctx.bot_manager.bot_for_shop(shop.shop_id) if ctx.bot_manager else None
        if bot is None:
            logger.warning("[WEB] shop %s bot not running — photos skipped", shop.shop_id)
            return file_ids
        for f in files:
            if not f or not f.filename:
                continue
            raw = await f.read()
            if not raw or len(raw) > MAX_IMAGE_BYTES:
                continue
            try:
                msg = await bot.send_photo(
                    shop.admin_id,
                    BufferedInputFile(raw, filename=f.filename),
                    caption="📷 uploaded from the dashboard",
                )
                file_ids.append(msg.photo[-1].file_id)
            except Exception as e:
                logger.warning("[WEB] photo upload failed for %s: %s", shop.shop_id, e)
        return file_ids

    # --------------------------------------------------------------- login

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return render(request, "login.html",
                      bot_username=ctx.master_bot_username or "")

    @app.get("/auth/telegram")
    async def auth_telegram(request: Request):
        data = dict(request.query_params)
        user = verify_telegram_login(data, ctx.config.bot_token)
        if not user:
            return render(request, "login.html", bot_username=ctx.master_bot_username or "",
                          error="That sign-in link could not be verified. Open the dashboard "
                                "from your browser and tap the Telegram button again.")
        token = make_session(user, ctx.config.web_session_secret)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax",
                        max_age=60 * 60 * 24 * 30,
                        secure=ctx.config.web_base_url.startswith("https"))
        return resp

    @app.get("/logout")
    async def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE_NAME)
        return resp

    # ----------------------------------------------------------- shop list

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        user = current_user(request)
        if not user:
            return need_login()
        async with ctx.db() as session:
            owned = await session.execute(select(Shop).where(Shop.admin_id == user["uid"]))
            shops = list(owned.scalars().all())
            co = await session.execute(
                select(Shop).join(StoreAdmin, StoreAdmin.shop_id == Shop.shop_id)
                .where(StoreAdmin.user_id == user["uid"], Shop.admin_id != user["uid"])
            )
            shops += list(co.scalars().all())
            stats = {}
            for s in shops:
                pending = await session.execute(
                    select(func.count(Order.id)).where(
                        Order.shop_id == s.shop_id,
                        Order.status.in_(("awaiting_verification", "awaiting_topup")))
                )
                stats[s.shop_id] = pending.scalar() or 0
        return render(request, "shops.html", shops=shops, stats=stats, now=utcnow())

    # ------------------------------------------------------------- catalog

    @app.get("/shop/{shop_id}", response_class=HTMLResponse)
    async def catalog(request: Request, shop_id: str):
        user = current_user(request)
        if not user:
            return need_login()
        async with ctx.db() as session:
            shop = await owned_shop(session, shop_id, user["uid"])
            if not shop:
                return RedirectResponse("/", status_code=303)
            settings = await get_settings(session, shop_id)
            cats = list((await session.execute(
                select(Category).where(Category.shop_id == shop_id).order_by(Category.name)
            )).scalars().all())
            prods = list((await session.execute(
                select(Product).where(Product.shop_id == shop_id).order_by(Product.title)
            )).scalars().all())
        by_cat: dict[int, list] = {c.id: [] for c in cats}
        for p in prods:
            by_cat.setdefault(p.category_id, []).append(p)
        return render(request, "catalog.html", shop=shop, settings=settings,
                      categories=cats, by_cat=by_cat, now=utcnow())

    @app.post("/shop/{shop_id}/category")
    async def add_category(request: Request, shop_id: str, name: str = Form(...)):
        user = current_user(request)
        if not user:
            return need_login()
        async with ctx.db() as session:
            shop = await owned_shop(session, shop_id, user["uid"])
            if shop and name.strip():
                session.add(Category(shop_id=shop_id, name=name.strip()[:64]))
                await session.commit()
        return RedirectResponse(f"/shop/{shop_id}", status_code=303)

    @app.post("/shop/{shop_id}/product")
    async def add_product(request: Request, shop_id: str,
                          category_id: int = Form(...), title: str = Form(...),
                          price: int = Form(...), description: str = Form(""),
                          photos: list[UploadFile] = None):
        user = current_user(request)
        if not user:
            return need_login()
        async with ctx.db() as session:
            shop = await owned_shop(session, shop_id, user["uid"])
            if not shop:
                return RedirectResponse("/", status_code=303)
            file_ids = await upload_to_telegram(shop, photos or [])
            session.add(Product(
                shop_id=shop_id, category_id=category_id, title=title.strip()[:128],
                price=max(int(price), 0), description=(description or "").strip()[:1024],
                media_file_ids=json.dumps(file_ids) if file_ids else None,
                available=1,
            ))
            await session.commit()
        return RedirectResponse(f"/shop/{shop_id}", status_code=303)

    @app.post("/shop/{shop_id}/product/{product_id}/stock")
    async def toggle_stock(request: Request, shop_id: str, product_id: int):
        user = current_user(request)
        if not user:
            return need_login()
        async with ctx.db() as session:
            shop = await owned_shop(session, shop_id, user["uid"])
            product = await session.get(Product, product_id)
            if shop and product and product.shop_id == shop_id:
                product.available = 0 if product.available else 1
                await session.commit()
        return RedirectResponse(f"/shop/{shop_id}", status_code=303)

    @app.post("/shop/{shop_id}/product/{product_id}/delete")
    async def delete_product(request: Request, shop_id: str, product_id: int):
        user = current_user(request)
        if not user:
            return need_login()
        async with ctx.db() as session:
            shop = await owned_shop(session, shop_id, user["uid"])
            product = await session.get(Product, product_id)
            if shop and product and product.shop_id == shop_id:
                await session.delete(product)
                await session.commit()
        return RedirectResponse(f"/shop/{shop_id}", status_code=303)

    # -------------------------------------------------------------- orders

    @app.get("/shop/{shop_id}/orders", response_class=HTMLResponse)
    async def orders(request: Request, shop_id: str, show: str = "open"):
        user = current_user(request)
        if not user:
            return need_login()
        async with ctx.db() as session:
            shop = await owned_shop(session, shop_id, user["uid"])
            if not shop:
                return RedirectResponse("/", status_code=303)
            q = select(Order).where(Order.shop_id == shop_id)
            if show == "open":
                q = q.where(Order.status.in_(
                    ("pending", "awaiting_verification", "awaiting_topup", "paid")))
            rows = list((await session.execute(
                q.order_by(Order.created_at.desc()).limit(100))).scalars().all())
            items: dict[int, list] = {}
            for o in rows:
                items[o.id] = list((await session.execute(
                    select(OrderItem).where(OrderItem.order_id == o.id)
                )).scalars().all())
            owed = (await session.execute(
                select(func.coalesce(func.sum(Order.refund_owed), 0))
                .where(Order.shop_id == shop_id, Order.refund_owed > 0)
            )).scalar() or 0
        return render(request, "orders.html", shop=shop, orders=rows,
                      items=items, show=show, refund_owed=owed)

    @app.post("/shop/{shop_id}/order/{order_id}/collected")
    async def mark_collected(request: Request, shop_id: str, order_id: int,
                             show: str = Form("open")):
        user = current_user(request)
        if not user:
            return need_login()
        async with ctx.db() as session:
            shop = await owned_shop(session, shop_id, user["uid"])
            order = await session.get(Order, order_id)
            if shop and order and order.shop_id == shop_id and order.status == "paid":
                order.status = "fulfilled"
                order.collected_at = utcnow()
                order.updated_at = utcnow()
                await session.commit()
        return RedirectResponse(f"/shop/{shop_id}/orders?show={show}", status_code=303)

    # ------------------------------------------------------------ settings

    @app.get("/shop/{shop_id}/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, shop_id: str, saved: int = 0):
        user = current_user(request)
        if not user:
            return need_login()
        async with ctx.db() as session:
            shop = await owned_shop(session, shop_id, user["uid"])
            if not shop:
                return RedirectResponse("/", status_code=303)
            settings = await get_settings(session, shop_id)
        return render(request, "settings.html", shop=shop, settings=settings,
                      saved=bool(saved), price_stars=ctx.config.subscription_price_stars)

    @app.post("/shop/{shop_id}/settings")
    async def save_settings(request: Request, shop_id: str,
                            bank_name: str = Form(""), account_name: str = Form(""),
                            account_number: str = Form(""), pickup_address: str = Form(""),
                            currency: str = Form("ETB"),
                            topup_timeout_minutes: int = Form(60),
                            payment_instructions: str = Form("")):
        user = current_user(request)
        if not user:
            return need_login()
        async with ctx.db() as session:
            shop = await owned_shop(session, shop_id, user["uid"])
            if not shop:
                return RedirectResponse("/", status_code=303)
            st = await get_settings(session, shop_id)
            st.bank_name = bank_name.strip()[:64] or None
            st.account_name = account_name.strip()[:128] or None
            st.account_number = account_number.strip()[:64] or None
            st.pickup_address = pickup_address.strip()[:256] or None
            st.currency = (currency.strip()[:8] or "ETB").upper()
            st.topup_timeout_minutes = max(int(topup_timeout_minutes), 5)
            st.payment_instructions = payment_instructions.strip()[:1024] or None
            await session.commit()
            await ctx.cache.invalidate_shop(shop_id) if hasattr(ctx.cache, "invalidate_shop") else None
        return RedirectResponse(f"/shop/{shop_id}/settings?saved=1", status_code=303)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app
