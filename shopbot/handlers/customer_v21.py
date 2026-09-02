"""Shop bot: customer side (v2.1) — browse, cart, checkout, screenshot payment.

Flow: browse categories → ➕ add to cart → 🛒 cart → ✅ checkout →
pickup time → phone (request_contact) → payment instructions →
customer sends SCREENSHOT → admins get a card with the photo attached
(Confirm / Payment short / Fake — see orders_admin.py).

Top-up: while an order is `awaiting_topup`, any photo the customer sends is
attached to it automatically as the next payment (no FSM needed, so nothing
is lost if they come back hours later).
"""
import logging
import random

from aiogram import Bot, F, Router, types
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)

from context import AppContext
from core.i18n import LANG_NAMES, SUPPORTED, get_lang, set_lang, t
from models import Product
from shopbot.common import (
    PRODUCTS_PER_PAGE, count_products, load_categories, load_products_page, media_ids,
)
from shopbot.states import Checkout

logger = logging.getLogger(__name__)
router = Router(name="shopbot.customer_v21")


# ------------------------------------------------------------------ helpers

async def _lang(ctx: AppContext, user: types.User) -> str:
    return await get_lang(ctx.redis, user.id, user.language_code)


def _cart_total(cart: list[dict]) -> int:
    return sum(int(i["price"]) * int(i["qty"]) for i in cart)


def _cart_count(cart: list[dict]) -> int:
    return sum(int(i["qty"]) for i in cart)


async def _get_cart(state: FSMContext) -> list[dict]:
    data = await state.get_data()
    return data.get("cart", [])


async def _main_keyboard(ctx: AppContext, shop_id: str, lang: str,
                         cart: list[dict]) -> InlineKeyboardMarkup:
    async with ctx.db() as session:
        categories = await load_categories(session, shop_id)
    rows = [
        [InlineKeyboardButton(text=f"📂 {c.name}", callback_data=f"cat_{c.id}_1")]
        for c in categories
    ]
    rows.append([InlineKeyboardButton(text=t("btn_cart", lang, n=_cart_count(cart)),
                                      callback_data="cart")])
    rows.append([InlineKeyboardButton(text=t("btn_help", lang), callback_data="shop_help"),
                 InlineKeyboardButton(text=t("btn_contact", lang), callback_data="contact_admin"),
                 InlineKeyboardButton(text=t("btn_lang", lang), callback_data="lang")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_welcome(message: types.Message, ctx: AppContext, shop_id: str,
                        user: types.User, state: FSMContext):
    lang = await _lang(ctx, user)
    cart = await _get_cart(state)
    async with ctx.db() as session:
        shop = await ctx.shops.load_shop(session, shop_id)
        await ctx.shops.increment_analytics(session, shop_id)
    name = shop.get("name") if shop else "our shop"
    await message.answer(
        t("welcome", lang, name=name), parse_mode="HTML",
        reply_markup=await _main_keyboard(ctx, shop_id, lang, cart),
    )


# -------------------------------------------------------------------- start

@router.message(CommandStart(deep_link=True))
async def start_with_payload(message: types.Message, command: CommandObject,
                             ctx: AppContext, shop_id: str, bot: Bot, state: FSMContext):
    payload = (command.args or "").strip()
    if payload.startswith("pay_shop_"):
        from shopbot.handlers.affiliate_payment import begin_affiliate_payment
        await begin_affiliate_payment(message, payload, ctx, shop_id, bot)
        return
    await _send_welcome(message, ctx, shop_id, message.from_user, state)


@router.message(CommandStart())
async def start_plain(message: types.Message, ctx: AppContext, shop_id: str, state: FSMContext):
    await _send_welcome(message, ctx, shop_id, message.from_user, state)


@router.message(Command("help"))
async def help_cmd(message: types.Message, ctx: AppContext):
    lang = await _lang(ctx, message.from_user)
    await message.answer(t("help", lang))


@router.callback_query(F.data == "shop_help")
async def help_cb(query: types.CallbackQuery, ctx: AppContext):
    await query.answer()
    lang = await _lang(ctx, query.from_user)
    await query.message.answer(t("help", lang))


@router.callback_query(F.data == "back_shop_main")
async def back_to_main(query: types.CallbackQuery, ctx: AppContext, shop_id: str, state: FSMContext):
    await query.answer()
    lang = await _lang(ctx, query.from_user)
    cart = await _get_cart(state)
    async with ctx.db() as session:
        shop = await ctx.shops.load_shop(session, shop_id)
    await query.message.answer(
        t("welcome", lang, name=shop.get("name") if shop else "Shop"), parse_mode="HTML",
        reply_markup=await _main_keyboard(ctx, shop_id, lang, cart),
    )


@router.callback_query(F.data == "contact_admin")
async def contact_admin(query: types.CallbackQuery, ctx: AppContext, shop_id: str):
    await query.answer()
    lang = await _lang(ctx, query.from_user)
    async with ctx.db() as session:
        shop = await ctx.shops.get_shop(session, shop_id)
        owner_id = shop.admin_id if shop else None
    if owner_id:
        try:
            chat = await ctx.master_bot.get_chat(owner_id)
            if chat.username:
                await query.message.answer(t("contact_admin", lang, username=chat.username))
                return
        except Exception:
            pass
    await query.message.answer(t("contact_admin_none", lang))


# ----------------------------------------------------------------- language

@router.callback_query(F.data == "lang")
@router.message(Command("language"))
async def language_menu(event: types.CallbackQuery | types.Message, ctx: AppContext):
    user = event.from_user
    lang = await _lang(ctx, user)
    rows = [[InlineKeyboardButton(text=LANG_NAMES[c], callback_data=f"setlang_{c}")]
            for c in SUPPORTED]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    target = event.message if isinstance(event, types.CallbackQuery) else event
    if isinstance(event, types.CallbackQuery):
        await event.answer()
    await target.answer(t("lang_q", lang), reply_markup=kb)


@router.callback_query(F.data.startswith("setlang_"))
async def language_set(query: types.CallbackQuery, ctx: AppContext):
    code = query.data[len("setlang_"):]
    await set_lang(ctx.redis, query.from_user.id, code)
    await query.answer()
    await query.message.answer(t("lang_set", code, name=LANG_NAMES.get(code, code)))


# -------------------------------------------------------------------- browse

@router.callback_query(F.data.startswith("cat_"))
async def browse_category(query: types.CallbackQuery, ctx: AppContext, shop_id: str):
    await query.answer()
    lang = await _lang(ctx, query.from_user)
    try:
        _, cat_id, page = query.data.split("_")
        cat_id, page = int(cat_id), int(page)
    except ValueError:
        return
    async with ctx.db() as session:
        products = [p for p in await load_products_page(session, cat_id, page)
                    if getattr(p, "available", 1)]
        total = await count_products(session, cat_id)
        currency, _ = await ctx.orders.get_shop_currency_and_instructions(session, shop_id)

    if not products:
        await query.message.answer(t("empty_category", lang))
        return

    rows = [
        [InlineKeyboardButton(text=f"🛍 {p.title} — {p.price} {currency}",
                              callback_data=f"prod_{p.id}")]
        for p in products
    ]
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text=t("btn_prev", lang), callback_data=f"cat_{cat_id}_{page - 1}"))
    if page * PRODUCTS_PER_PAGE < total:
        nav.append(InlineKeyboardButton(text=t("btn_next", lang), callback_data=f"cat_{cat_id}_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_shop_main")])
    await query.message.answer(f"📂 ({total})", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("prod_"))
async def product_details(query: types.CallbackQuery, ctx: AppContext, shop_id: str, bot: Bot):
    await query.answer()
    lang = await _lang(ctx, query.from_user)
    try:
        product_id = int(query.data[len("prod_"):])
    except ValueError:
        return
    async with ctx.db() as session:
        product = await session.get(Product, product_id)
        if not product or product.shop_id != shop_id:
            await query.message.answer(t("adm_not_found", lang))
            return
        currency, _ = await ctx.orders.get_shop_currency_and_instructions(session, shop_id)

    photos = media_ids(product)
    if photos:
        try:
            media = [InputMediaPhoto(media=fid) for fid in photos[:10]]
            await bot.send_media_group(query.message.chat.id, media)
        except Exception as e:
            logger.warning("[SHOP:%s] media send failed for product %s: %s", shop_id, product_id, e)

    text = f"🛍 <b>{product.title}</b>\n💰 <b>{product.price} {currency}</b>"
    if product.description:
        text += f"\n\n{product.description}"
    await query.message.answer(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_add", lang, price=product.price, cur=currency),
                                  callback_data=f"addcart_{product.id}")],
            [InlineKeyboardButton(text=t("btn_back", lang), callback_data=f"cat_{product.category_id}_1")],
        ]),
    )


# ---------------------------------------------------------------------- cart

@router.callback_query(F.data.startswith("addcart_"))
async def add_to_cart(query: types.CallbackQuery, ctx: AppContext, shop_id: str, state: FSMContext):
    lang = await _lang(ctx, query.from_user)
    try:
        product_id = int(query.data[len("addcart_"):])
    except ValueError:
        return
    async with ctx.db() as session:
        product = await session.get(Product, product_id)
        if not product or product.shop_id != shop_id or not getattr(product, "available", 1):
            await query.answer(t("adm_not_found", lang), show_alert=True)
            return
        currency, _ = await ctx.orders.get_shop_currency_and_instructions(session, shop_id)

    cart = await _get_cart(state)
    for item in cart:
        if item["product_id"] == product_id:
            item["qty"] = int(item["qty"]) + 1
            break
    else:
        cart.append({"product_id": product_id, "title": product.title,
                     "price": int(product.price or 0), "qty": 1})
    await state.update_data(cart=cart)
    await query.answer("✅")
    await query.message.answer(
        t("added", lang, title=product.title, n=_cart_count(cart),
          total=_cart_total(cart), cur=currency),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_view_cart", lang), callback_data="cart"),
             InlineKeyboardButton(text=t("btn_checkout", lang), callback_data="checkout")],
            [InlineKeyboardButton(text=t("btn_continue", lang), callback_data="back_shop_main")],
        ]),
    )


async def _render_cart(target: types.Message, ctx: AppContext, shop_id: str,
                       lang: str, cart: list[dict]):
    async with ctx.db() as session:
        currency, _ = await ctx.orders.get_shop_currency_and_instructions(session, shop_id)
    if not cart:
        await target.answer(t("cart_empty", lang), reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=t("btn_continue", lang),
                                                   callback_data="back_shop_main")]]))
        return
    lines = [t("order_line", lang, qty=i["qty"], title=i["title"],
               sum=int(i["price"]) * int(i["qty"]), cur=currency) for i in cart]
    text = t("cart_title", lang) + "\n\n" + "\n".join(lines) + "\n\n" + \
        t("cart_total", lang, total=_cart_total(cart), cur=currency)
    rows = [
        [InlineKeyboardButton(text=t("btn_minus", lang), callback_data=f"qty_{idx}_-1"),
         InlineKeyboardButton(text=f"{i['qty']}× {i['title'][:20]}", callback_data="noop"),
         InlineKeyboardButton(text=t("btn_plus", lang), callback_data=f"qty_{idx}_1")]
        for idx, i in enumerate(cart)
    ]
    rows.append([InlineKeyboardButton(text=t("btn_checkout", lang), callback_data="checkout")])
    rows.append([InlineKeyboardButton(text=t("btn_clear", lang), callback_data="cart_clear"),
                 InlineKeyboardButton(text=t("btn_continue", lang), callback_data="back_shop_main")])
    await target.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "noop")
async def noop(query: types.CallbackQuery):
    await query.answer()


@router.callback_query(F.data == "cart")
async def view_cart(query: types.CallbackQuery, ctx: AppContext, shop_id: str, state: FSMContext):
    await query.answer()
    lang = await _lang(ctx, query.from_user)
    await _render_cart(query.message, ctx, shop_id, lang, await _get_cart(state))


@router.callback_query(F.data == "cart_clear")
async def clear_cart(query: types.CallbackQuery, ctx: AppContext, shop_id: str, state: FSMContext):
    await query.answer()
    lang = await _lang(ctx, query.from_user)
    await state.update_data(cart=[])
    await _render_cart(query.message, ctx, shop_id, lang, [])


@router.callback_query(F.data.startswith("qty_"))
async def change_qty(query: types.CallbackQuery, ctx: AppContext, shop_id: str, state: FSMContext):
    await query.answer()
    lang = await _lang(ctx, query.from_user)
    try:
        _, idx, delta = query.data.split("_")
        idx, delta = int(idx), int(delta)
    except ValueError:
        return
    cart = await _get_cart(state)
    if 0 <= idx < len(cart):
        cart[idx]["qty"] = int(cart[idx]["qty"]) + delta
        if cart[idx]["qty"] <= 0:
            cart.pop(idx)
        await state.update_data(cart=cart)
    await _render_cart(query.message, ctx, shop_id, lang, cart)


# ------------------------------------------------------------------ checkout

@router.callback_query(F.data == "checkout")
async def checkout(query: types.CallbackQuery, ctx: AppContext, shop_id: str, state: FSMContext):
    await query.answer()
    lang = await _lang(ctx, query.from_user)
    cart = await _get_cart(state)
    if not cart:
        await query.message.answer(t("cart_empty", lang))
        return
    await query.message.answer(
        t("pickup_q", lang),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t("pickup_asap", lang), callback_data="pickup_asap")],
            [InlineKeyboardButton(text=t("pickup_today", lang), callback_data="pickup_today"),
             InlineKeyboardButton(text=t("pickup_tomorrow", lang), callback_data="pickup_tomorrow")],
            [InlineKeyboardButton(text=t("pickup_custom", lang), callback_data="pickup_custom")],
        ]),
    )


async def _ask_phone(target: types.Message, lang: str, state: FSMContext, pickup: str):
    await state.update_data(pickup=pickup)
    await state.set_state(Checkout.waiting_phone)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t("btn_share_phone", lang), request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await target.answer(t("phone_q", lang), reply_markup=kb)


@router.callback_query(F.data.in_({"pickup_asap", "pickup_today", "pickup_tomorrow"}))
async def pickup_quick(query: types.CallbackQuery, ctx: AppContext, state: FSMContext):
    await query.answer()
    lang = await _lang(ctx, query.from_user)
    await _ask_phone(query.message, lang, state, t(query.data, lang))


@router.callback_query(F.data == "pickup_custom")
async def pickup_custom(query: types.CallbackQuery, ctx: AppContext, state: FSMContext):
    await query.answer()
    lang = await _lang(ctx, query.from_user)
    await state.set_state(Checkout.waiting_pickup_text)
    await query.message.answer(t("pickup_type", lang))


@router.message(Checkout.waiting_pickup_text, F.text)
async def pickup_typed(message: types.Message, ctx: AppContext, state: FSMContext):
    lang = await _lang(ctx, message.from_user)
    await _ask_phone(message, lang, state, message.text.strip()[:64])


def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit() or ch == "+")


@router.message(Checkout.waiting_phone, F.contact)
async def phone_contact(message: types.Message, ctx: AppContext, shop_id: str,
                        state: FSMContext, bot: Bot):
    await _finish_checkout(message, ctx, shop_id, state, bot,
                           phone=message.contact.phone_number)


@router.message(Checkout.waiting_phone, F.text)
async def phone_typed(message: types.Message, ctx: AppContext, shop_id: str,
                      state: FSMContext, bot: Bot):
    lang = await _lang(ctx, message.from_user)
    phone = _digits(message.text or "")
    if len(phone) < 7:
        await message.answer(t("phone_invalid", lang))
        return
    await _finish_checkout(message, ctx, shop_id, state, bot, phone=phone)


async def _finish_checkout(message: types.Message, ctx: AppContext, shop_id: str,
                           state: FSMContext, bot: Bot, phone: str):
    user = message.from_user
    lang = await _lang(ctx, user)
    data = await state.get_data()
    cart, pickup = data.get("cart", []), data.get("pickup", "-")
    if not cart:
        await state.clear()
        await message.answer(t("cart_empty", lang), reply_markup=ReplyKeyboardRemove())
        return

    full_name = " ".join(x for x in (user.first_name, user.last_name) if x) or str(user.id)
    async with ctx.db() as session:
        currency, instructions = await ctx.orders.get_shop_currency_and_instructions(session, shop_id)
        order = await ctx.orders.create_cart_order(
            session, shop_id=shop_id, items=cart, customer_id=user.id,
            customer_username=user.username, customer_name=full_name,
            customer_phone=phone, pickup_time=pickup, currency=currency,
        )

    # cart is done; keep FSM data minimal for the screenshot step
    await state.set_state(Checkout.waiting_screenshot)
    await state.update_data(cart=[], order_id=order.id)

    if instructions:
        text = t("pay_text", lang, id=order.id, total=order.amount,
                 cur=currency, instructions=instructions)
    else:
        text = t("pay_text_noinstr", lang, id=order.id, total=order.amount, cur=currency)
    await message.answer(text, reply_markup=ReplyKeyboardRemove())


@router.message(Checkout.waiting_screenshot, F.photo)
async def first_screenshot(message: types.Message, ctx: AppContext, shop_id: str,
                           state: FSMContext, bot: Bot):
    data = await state.get_data()
    order_id = data.get("order_id")
    await state.clear()
    if not order_id:
        return
    await _attach_screenshot(message, ctx, shop_id, bot, order_id)


@router.message(Checkout.waiting_screenshot)
async def first_screenshot_wrong_type(message: types.Message, ctx: AppContext):
    lang = await _lang(ctx, message.from_user)
    await message.answer(t("screenshot_only", lang))


# Photos outside any FSM state: top-up for the newest open order (survives
# restarts and long delays — no state required).
@router.message(F.photo)
async def loose_photo(message: types.Message, ctx: AppContext, shop_id: str, bot: Bot):
    async with ctx.db() as session:
        order = await ctx.orders.latest_open_order(session, shop_id, message.from_user.id)
    if not order:
        lang = await _lang(ctx, message.from_user)
        await message.answer(t("no_open_order", lang))
        return
    await _attach_screenshot(message, ctx, shop_id, bot, order.id)


async def _attach_screenshot(message: types.Message, ctx: AppContext, shop_id: str,
                             bot: Bot, order_id: int):
    """Store the screenshot and send the admin card with the photo attached."""
    from shopbot.handlers.orders_admin import send_admin_card  # circular-safe
    lang = await _lang(ctx, message.from_user)
    file_id = message.photo[-1].file_id
    async with ctx.db() as session:
        payment = await ctx.orders.add_payment(session, order_id, file_id)
        if not payment:
            return
        order = await ctx.orders.get_order(session, order_id)
        items = await ctx.orders.get_items(session, order_id)
        admin_ids = await ctx.orders.get_admin_ids(session, shop_id)

    key = "order_sent" if payment.seq == 1 else "topup_received"
    await message.answer(t(key, lang, id=order_id))
    await send_admin_card(ctx, bot, order, items, admin_ids, file_id, payment.seq)
