"""Shop-owner commands for the daily counter routine.

These are deliberately terse — they get used one-handed during a rush:

    /today              what came in since midnight
    /collect 4821       hand an order over by pickup code
    /stock              toggle a product in/out of stock
    /dashboard          link to the web dashboard

Anything that needs typing more than a few characters lives on the website
instead.
"""
import logging

from aiogram import Bot, F, Router, types
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from context import AppContext
from core.i18n import get_lang, t
from models import Product
from shopbot.common import load_categories

logger = logging.getLogger(__name__)
router = Router(name="shopbot.owner")

_STATUS_LABEL = {
    "pending": "…", "awaiting_verification": "check", "awaiting_topup": "short",
    "paid": "paid", "fulfilled": "collected", "cancelled": "cancelled",
}


async def _is_admin(ctx: AppContext, shop_id: str, user_id: int) -> bool:
    async with ctx.db() as session:
        return user_id in await ctx.orders.get_admin_ids(session, shop_id)


@router.message(Command("today"))
async def today(message: types.Message, ctx: AppContext, shop_id: str):
    lang = await get_lang(ctx.redis, message.from_user.id, message.from_user.language_code)
    if not await _is_admin(ctx, shop_id, message.from_user.id):
        return
    async with ctx.db() as session:
        orders = await ctx.orders.today_orders(session, shop_id)
    if not orders:
        await message.answer(t("today_none", lang))
        return
    lines = [t("today_title", lang, n=len(orders)), ""]
    takings = 0
    for o in orders:
        if o.status in ("paid", "fulfilled"):
            takings += o.amount
        lines.append(t("today_line", lang, id=o.id,
                       name=o.customer_name or o.customer_username or o.customer_id,
                       total=o.amount, cur=o.currency,
                       status=_STATUS_LABEL.get(o.status, o.status)))
    lines.append("")
    lines.append(t("adm_total", lang, total=takings, cur=orders[0].currency))
    await message.answer("\n".join(lines))


@router.message(Command("collect"))
async def collect(message: types.Message, command: CommandObject, ctx: AppContext, shop_id: str):
    lang = await get_lang(ctx.redis, message.from_user.id, message.from_user.language_code)
    if not await _is_admin(ctx, shop_id, message.from_user.id):
        return
    code = "".join(ch for ch in (command.args or "") if ch.isdigit())[:4]
    if len(code) != 4:
        await message.answer(t("collect_bad", lang))
        return
    async with ctx.db() as session:
        order = await ctx.orders.by_pickup_code(session, shop_id, code)
        if not order:
            await message.answer(t("collect_bad", lang))
            return
        items = await ctx.orders.get_items(session, order.id)
        await ctx.orders.mark_collected(session, order.id)
    detail = "\n".join(t("order_line", lang, qty=i.qty, title=i.title,
                         sum=i.unit_price * i.qty, cur=order.currency) for i in items)
    await message.answer(
        t("collect_ok", lang, id=order.id,
          name=order.customer_name or order.customer_username or order.customer_id)
        + ("\n\n" + detail if detail else "")
    )


@router.message(Command("stock"))
async def stock_menu(message: types.Message, ctx: AppContext, shop_id: str):
    lang = await get_lang(ctx.redis, message.from_user.id, message.from_user.language_code)
    if not await _is_admin(ctx, shop_id, message.from_user.id):
        return
    async with ctx.db() as session:
        cats = await load_categories(session, shop_id)
        rows = []
        for c in cats:
            from sqlalchemy import select
            prods = (await session.execute(
                select(Product).where(Product.category_id == c.id).order_by(Product.title)
            )).scalars().all()
            for p in prods:
                mark = "🟢" if p.available else "⚪️"
                rows.append([InlineKeyboardButton(
                    text=f"{mark} {p.title}", callback_data=f"stk_{p.id}")])
    if not rows:
        await message.answer(t("empty_category", lang))
        return
    await message.answer("🟢 = customers can order it\n⚪️ = hidden",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows[:60]))


@router.callback_query(F.data.startswith("stk_"))
async def stock_toggle(query: types.CallbackQuery, ctx: AppContext, shop_id: str):
    if not await _is_admin(ctx, shop_id, query.from_user.id):
        await query.answer()
        return
    try:
        product_id = int(query.data[len("stk_"):])
    except ValueError:
        return
    async with ctx.db() as session:
        product = await session.get(Product, product_id)
        if not product or product.shop_id != shop_id:
            await query.answer()
            return
        product.available = 0 if product.available else 1
        await session.commit()
        state = product.available
        title = product.title
    await query.answer("🟢" if state else "⚪️")
    if query.message.reply_markup:
        rows = []
        for row in query.message.reply_markup.inline_keyboard:
            new_row = []
            for btn in row:
                if btn.callback_data == query.data:
                    new_row.append(InlineKeyboardButton(
                        text=f"{'🟢' if state else '⚪️'} {title}", callback_data=btn.callback_data))
                else:
                    new_row.append(btn)
            rows.append(new_row)
        try:
            await query.message.edit_reply_markup(
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except Exception:
            pass


@router.message(Command("dashboard"))
async def dashboard(message: types.Message, ctx: AppContext, shop_id: str):
    lang = await get_lang(ctx.redis, message.from_user.id, message.from_user.language_code)
    if not await _is_admin(ctx, shop_id, message.from_user.id):
        return
    await message.answer(t("dashboard_link", lang, url=ctx.config.web_base_url),
                         disable_web_page_preview=False)
