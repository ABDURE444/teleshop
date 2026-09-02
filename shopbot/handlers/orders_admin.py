"""Admin side of the v2.1 order flow.

Every screenshot produces a card in each admin's chat: the photo, the
order lines, customer name + phone, pickup time, and three buttons:

    ✅ Confirm        → order paid, 4-digit pickup code sent to the customer
    ⚠️ Payment short  → admin types the received amount; the customer is told
                        the exact remaining sum and sends another screenshot
    ❌ Fake           → order cancelled, customer notified

Multiple admins may receive the card; the first action wins (status checks
make the buttons idempotent).
"""
import logging
import random

from aiogram import Bot, F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from context import AppContext
from core.i18n import get_lang, t
from shopbot.states import AdminShort

logger = logging.getLogger(__name__)
router = Router(name="shopbot.orders_admin")


def _card_text(order, items, lang: str, seq: int) -> str:
    header = t("adm_new_order", lang, id=order.id) if seq == 1 else \
        t("adm_topup", lang, id=order.id, seq=seq)
    lines = [t("order_line", lang, qty=i.qty, title=i.title,
               sum=i.unit_price * i.qty, cur=order.currency) for i in items]
    if not lines and order.product_title:  # legacy single-product orders
        lines = [t("order_line", lang, qty=order.quantity, title=order.product_title,
                   sum=order.amount, cur=order.currency)]
    parts = [
        header,
        t("adm_customer", lang, name=order.customer_name or (order.customer_username or order.customer_id),
          phone=order.customer_phone or "—"),
        t("adm_pickup", lang, pickup=order.pickup_time or "—"),
        "",
        "\n".join(lines),
        "",
        t("adm_total", lang, total=order.amount, cur=order.currency),
    ]
    if order.paid_total:
        parts.append(t("adm_paid", lang, paid=order.paid_total, cur=order.currency))
    return "\n".join(parts)


def _card_keyboard(order_id: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("btn_adm_confirm", lang), callback_data=f"ordok_{order_id}")],
        [InlineKeyboardButton(text=t("btn_adm_short", lang), callback_data=f"ordshort_{order_id}"),
         InlineKeyboardButton(text=t("btn_adm_fake", lang), callback_data=f"ordfake_{order_id}")],
    ])


async def send_admin_card(ctx: AppContext, bot: Bot, order, items,
                          admin_ids: list[int], screenshot_file_id: str, seq: int):
    for admin_id in admin_ids:
        lang = await get_lang(ctx.redis, admin_id)
        try:
            await bot.send_photo(
                admin_id, screenshot_file_id,
                caption=_card_text(order, items, lang, seq),
                reply_markup=_card_keyboard(order.id, lang),
            )
        except Exception as e:
            logger.debug("[SHOP:%s] admin %s card failed: %s", order.shop_id, admin_id, e)


async def _notify_customer(ctx: AppContext, bot: Bot, order, key: str, **kwargs):
    lang = await get_lang(ctx.redis, order.customer_id)
    try:
        await bot.send_message(order.customer_id, t(key, lang, **kwargs))
    except Exception as e:
        logger.warning("[ORDER#%s] customer notify failed: %s", order.id, e)


# ------------------------------------------------------------------- actions

@router.callback_query(F.data.startswith("ordok_"))
async def confirm_order(query: types.CallbackQuery, ctx: AppContext, shop_id: str, bot: Bot):
    lang = await get_lang(ctx.redis, query.from_user.id)
    try:
        order_id = int(query.data[len("ordok_"):])
    except ValueError:
        return
    async with ctx.db() as session:
        order = await ctx.orders.get_order(session, order_id)
        if not order or order.shop_id != shop_id:
            await query.answer(t("adm_not_found", lang), show_alert=True)
            return
        if order.status in ("paid", "fulfilled", "cancelled"):
            await query.answer("✅")
            return
        code = f"{random.randint(0, 9999):04d}"
        order.pickup_code = code
        await ctx.orders.set_status(session, order_id, "paid")

    await query.answer("✅")
    await query.message.answer(t("adm_confirmed", lang, id=order_id, code=code))
    await _notify_customer(ctx, bot, order, "confirmed", id=order_id, code=code,
                           pickup=order.pickup_time or "—")


@router.callback_query(F.data.startswith("ordfake_"))
async def fake_order(query: types.CallbackQuery, ctx: AppContext, shop_id: str, bot: Bot):
    lang = await get_lang(ctx.redis, query.from_user.id)
    try:
        order_id = int(query.data[len("ordfake_"):])
    except ValueError:
        return
    async with ctx.db() as session:
        order = await ctx.orders.get_order(session, order_id)
        if not order or order.shop_id != shop_id:
            await query.answer(t("adm_not_found", lang), show_alert=True)
            return
        if order.status == "cancelled":
            await query.answer("❌")
            return
        await ctx.orders.set_status(session, order_id, "cancelled")

    await query.answer("❌")
    await query.message.answer(t("adm_faked", lang, id=order_id))
    await _notify_customer(ctx, bot, order, "fake_notice", id=order_id)


@router.callback_query(F.data.startswith("ordshort_"))
async def short_start(query: types.CallbackQuery, ctx: AppContext, shop_id: str,
                      state: FSMContext):
    lang = await get_lang(ctx.redis, query.from_user.id)
    try:
        order_id = int(query.data[len("ordshort_"):])
    except ValueError:
        return
    await query.answer()
    await state.set_state(AdminShort.waiting_amount)
    await state.update_data(short_order_id=order_id)
    await query.message.answer(t("adm_short_ask", lang, id=order_id))


@router.message(AdminShort.waiting_amount, F.text)
async def short_amount(message: types.Message, ctx: AppContext, shop_id: str,
                       state: FSMContext, bot: Bot):
    lang = await get_lang(ctx.redis, message.from_user.id)
    raw = (message.text or "").strip().replace(",", "").replace(" ", "")
    if not raw.isdigit():
        await message.answer(t("adm_invalid_number", lang))
        return
    amount = int(raw)
    data = await state.get_data()
    order_id = data.get("short_order_id")
    await state.clear()
    if not order_id:
        return

    async with ctx.db() as session:
        order = await ctx.orders.record_received(session, order_id, amount)
        if not order or order.shop_id != shop_id:
            await message.answer(t("adm_not_found", lang))
            return

    remaining = max(order.amount - order.paid_total, 0)
    if order.status == "paid":  # the recorded sum actually covers the total
        code = f"{random.randint(0, 9999):04d}"
        async with ctx.db() as session:
            o = await ctx.orders.get_order(session, order_id)
            o.pickup_code = code
            await session.commit()
        await message.answer(t("adm_confirmed", lang, id=order_id, code=code))
        await _notify_customer(ctx, bot, order, "confirmed", id=order_id, code=code,
                               pickup=order.pickup_time or "—")
        return

    await message.answer(t("adm_short_saved", lang, id=order_id, paid=order.paid_total,
                           remaining=remaining, cur=order.currency))
    await _notify_customer(ctx, bot, order, "short_notice", id=order_id,
                           total=order.amount, paid=order.paid_total,
                           remaining=remaining, cur=order.currency)
