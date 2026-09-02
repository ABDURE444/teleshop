"""Shop bot: admin panel — categories, products, admins, orders, settings.

All multi-step flows use Redis-backed FSM (v1 used in-memory dicts that lost
state on every restart)."""
import json
import logging

from aiogram import Bot, F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from context import AppContext
from models import Category, Product, ShopSettings, StoreAdmin
from shopbot.common import is_shop_admin, load_categories, count_products, parse_product_info
from shopbot.states import AddAdmin, AddCategory, AddProduct, EditProduct, PaymentSettings

logger = logging.getLogger(__name__)
router = Router(name="shopbot.admin")


async def _require_admin(ctx: AppContext, shop_id: str, user_id: int) -> bool:
    async with ctx.db() as session:
        return await is_shop_admin(session, shop_id, user_id, ctx.config.super_admin_id)


def _panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📂 Categories", callback_data="adm_cats"),
         InlineKeyboardButton(text="👥 Admins", callback_data="adm_admins")],
        [InlineKeyboardButton(text="🧾 Orders", callback_data="adm_orders"),
         InlineKeyboardButton(text="💳 Payment Settings", callback_data="adm_paysettings")],
    ])


@router.message(Command("admin"))
async def admin_command(message: types.Message, ctx: AppContext, shop_id: str):
    if not await _require_admin(ctx, shop_id, message.from_user.id):
        await message.answer("🚫 Admins only.")
        return
    await message.answer("🛠 <b>Admin panel</b>", parse_mode="HTML", reply_markup=_panel_keyboard())


@router.callback_query(F.data == "adm_panel")
async def admin_panel_cb(query: types.CallbackQuery, ctx: AppContext, shop_id: str):
    await query.answer()
    if not await _require_admin(ctx, shop_id, query.from_user.id):
        return
    await query.message.answer("🛠 <b>Admin panel</b>", parse_mode="HTML", reply_markup=_panel_keyboard())


# ---------------------------------------------------------------- categories

@router.callback_query(F.data == "adm_cats")
async def manage_categories(query: types.CallbackQuery, ctx: AppContext, shop_id: str):
    await query.answer()
    if not await _require_admin(ctx, shop_id, query.from_user.id):
        return
    async with ctx.db() as session:
        categories = await load_categories(session, shop_id)
        counts = {c.id: await count_products(session, c.id) for c in categories}
    rows = [
        [InlineKeyboardButton(text=f"📂 {c.name} ({counts[c.id]})", callback_data=f"adm_cat_{c.id}")]
        for c in categories
    ]
    rows.append([InlineKeyboardButton(text="➕ Add Category", callback_data="adm_addcat")])
    rows.append([InlineKeyboardButton(text="⬅️ Panel", callback_data="adm_panel")])
    await query.message.answer("📂 Categories:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "adm_addcat")
async def add_category(query: types.CallbackQuery, state: FSMContext, ctx: AppContext, shop_id: str):
    await query.answer()
    if not await _require_admin(ctx, shop_id, query.from_user.id):
        return
    await state.set_state(AddCategory.waiting_name)
    await query.message.answer("Send the new category name (or /cancel):")


@router.message(AddCategory.waiting_name, F.text)
async def category_name_received(message: types.Message, state: FSMContext, ctx: AppContext, shop_id: str):
    if message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return
    name = message.text.strip()[:64]
    await state.clear()
    async with ctx.db() as session:
        try:
            session.add(Category(shop_id=shop_id, name=name))
            await session.commit()
            await message.answer(f"✅ Category <b>{name}</b> created.", parse_mode="HTML")
        except IntegrityError:
            await session.rollback()
            await message.answer("❌ A category with that name already exists.")


@router.callback_query(F.data.startswith("adm_cat_"))
async def manage_category(query: types.CallbackQuery, ctx: AppContext, shop_id: str):
    await query.answer()
    if not await _require_admin(ctx, shop_id, query.from_user.id):
        return
    try:
        cat_id = int(query.data[len("adm_cat_"):])
    except ValueError:
        return
    async with ctx.db() as session:
        category = await session.get(Category, cat_id)
        if not category or category.shop_id != shop_id:
            return
        result = await session.execute(
            select(Product).where(Product.category_id == cat_id).order_by(Product.title).limit(30)
        )
        products = list(result.scalars().all())
    rows = [
        [InlineKeyboardButton(text=f"✏️ {p.title}", callback_data=f"adm_editprod_{p.id}"),
         InlineKeyboardButton(text="🗑", callback_data=f"adm_delprod_{p.id}")]
        for p in products
    ]
    rows.append([InlineKeyboardButton(text="➕ Add Product", callback_data=f"adm_addprod_{cat_id}")])
    rows.append([InlineKeyboardButton(text="🗑 Delete Category", callback_data=f"adm_delcat_{cat_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Categories", callback_data="adm_cats")])
    await query.message.answer(
        f"📂 <b>{category.name}</b> — {len(products)} product(s):",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("adm_delcat_"))
async def delete_category(query: types.CallbackQuery, ctx: AppContext, shop_id: str):
    await query.answer()
    if not await _require_admin(ctx, shop_id, query.from_user.id):
        return
    try:
        cat_id = int(query.data[len("adm_delcat_"):])
    except ValueError:
        return
    async with ctx.db() as session:
        category = await session.get(Category, cat_id)
        if not category or category.shop_id != shop_id:
            return
        name = category.name
        result = await session.execute(select(Product).where(Product.category_id == cat_id))
        for p in result.scalars().all():
            await session.delete(p)
        await session.delete(category)
        await session.commit()
    await query.message.answer(f"🗑 Category <b>{name}</b> and its products deleted.", parse_mode="HTML")


# ------------------------------------------------------------------ products

@router.callback_query(F.data.startswith("adm_addprod_"))
async def add_product(query: types.CallbackQuery, state: FSMContext, ctx: AppContext, shop_id: str):
    await query.answer()
    if not await _require_admin(ctx, shop_id, query.from_user.id):
        return
    try:
        cat_id = int(query.data[len("adm_addprod_"):])
    except ValueError:
        return
    await state.set_state(AddProduct.waiting_info)
    await state.update_data(category_id=cat_id)
    await query.message.answer(
        "Send the product as one line:\n\n"
        "<code>Name | Price | Description | Links</code>\n\n"
        "Example:\n<code>Scratch Course | 2500 | 7-week beginner course | https://example.com</code>\n"
        "(description and links are optional; /cancel to abort)",
        parse_mode="HTML",
    )


@router.message(AddProduct.waiting_info, F.text)
async def product_info_received(message: types.Message, state: FSMContext):
    if message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return
    parsed = parse_product_info(message.text)
    if isinstance(parsed, str):
        await message.answer(f"❌ {parsed}")
        return
    title, price, description, links = parsed
    await state.update_data(title=title, price=price, description=description, links=links, media=[])
    await state.set_state(AddProduct.waiting_media)
    await message.answer("📸 Now send product photos (optional). Type /done when finished.")


@router.message(AddProduct.waiting_media, F.photo)
async def product_media_received(message: types.Message, state: FSMContext):
    data = await state.get_data()
    media = data.get("media", [])
    if len(media) >= 10:
        await message.answer("Maximum 10 photos — type /done to save.")
        return
    media.append(message.photo[-1].file_id)
    await state.update_data(media=media)
    await message.answer(f"📸 {len(media)} photo(s) added. Send more or /done.")


@router.message(AddProduct.waiting_media, Command("done"))
async def product_save(message: types.Message, state: FSMContext, ctx: AppContext, shop_id: str):
    data = await state.get_data()
    await state.clear()
    async with ctx.db() as session:
        try:
            product = Product(
                shop_id=shop_id,
                category_id=data["category_id"],
                title=data["title"],
                price=data["price"],
                description=data.get("description") or "",
                links=data.get("links") or "",
                media_file_ids=json.dumps(data.get("media", [])),
            )
            session.add(product)
            await session.commit()
            await message.answer(f"✅ Product <b>{data['title']}</b> saved ({data['price']}).", parse_mode="HTML")
        except IntegrityError:
            await session.rollback()
            await message.answer("❌ A product with that name already exists in this category.")


@router.callback_query(F.data.startswith("adm_editprod_"))
async def edit_product(query: types.CallbackQuery, state: FSMContext, ctx: AppContext, shop_id: str):
    await query.answer()
    if not await _require_admin(ctx, shop_id, query.from_user.id):
        return
    try:
        product_id = int(query.data[len("adm_editprod_"):])
    except ValueError:
        return
    async with ctx.db() as session:
        product = await session.get(Product, product_id)
        if not product or product.shop_id != shop_id:
            return
    await state.set_state(EditProduct.waiting_info)
    await state.update_data(product_id=product_id)
    await query.message.answer(
        "Current values:\n"
        f"<code>{product.title} | {product.price} | {product.description or ''} | {product.links or ''}</code>\n\n"
        "Send the corrected line in the same format (or /cancel):",
        parse_mode="HTML",
    )


@router.message(EditProduct.waiting_info, F.text)
async def edit_product_received(message: types.Message, state: FSMContext, ctx: AppContext, shop_id: str):
    if message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return
    parsed = parse_product_info(message.text)
    if isinstance(parsed, str):
        await message.answer(f"❌ {parsed}")
        return
    data = await state.get_data()
    await state.clear()
    title, price, description, links = parsed
    async with ctx.db() as session:
        product = await session.get(Product, data.get("product_id"))
        if not product or product.shop_id != shop_id:
            await message.answer("❌ Product not found.")
            return
        product.title, product.price = title, price
        product.description, product.links = description, links
        try:
            await session.commit()
            await message.answer(f"✅ Product <b>{title}</b> updated.", parse_mode="HTML")
        except IntegrityError:
            await session.rollback()
            await message.answer("❌ A product with that name already exists in this category.")


@router.callback_query(F.data.startswith("adm_delprod_"))
async def delete_product(query: types.CallbackQuery, ctx: AppContext, shop_id: str):
    await query.answer()
    if not await _require_admin(ctx, shop_id, query.from_user.id):
        return
    try:
        product_id = int(query.data[len("adm_delprod_"):])
    except ValueError:
        return
    async with ctx.db() as session:
        product = await session.get(Product, product_id)
        if not product or product.shop_id != shop_id:
            return
        title = product.title
        await session.delete(product)
        await session.commit()
    await query.message.answer(f"🗑 Product <b>{title}</b> deleted.", parse_mode="HTML")


# -------------------------------------------------------------------- admins

@router.callback_query(F.data == "adm_admins")
async def manage_admins(query: types.CallbackQuery, ctx: AppContext, shop_id: str):
    await query.answer()
    if not await _require_admin(ctx, shop_id, query.from_user.id):
        return
    async with ctx.db() as session:
        result = await session.execute(select(StoreAdmin).where(StoreAdmin.shop_id == shop_id))
        admins = list(result.scalars().all())
    rows = [
        [InlineKeyboardButton(
            text=f"👤 {a.username or a.user_id} ({a.admin_type})",
            callback_data=f"adm_deladmin_{a.user_id}" if a.admin_type != "owner" else "adm_noop",
        )]
        for a in admins
    ]
    rows.append([InlineKeyboardButton(text="➕ Add Admin", callback_data="adm_addadmin")])
    rows.append([InlineKeyboardButton(text="⬅️ Panel", callback_data="adm_panel")])
    await query.message.answer(
        "👥 Admins (tap a non-owner admin to remove):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "adm_noop")
async def noop(query: types.CallbackQuery):
    await query.answer("The owner can't be removed.")


@router.callback_query(F.data == "adm_addadmin")
async def add_admin(query: types.CallbackQuery, state: FSMContext, ctx: AppContext, shop_id: str):
    await query.answer()
    if not await _require_admin(ctx, shop_id, query.from_user.id):
        return
    await state.set_state(AddAdmin.waiting_user)
    await query.message.answer(
        "Send the new admin's Telegram <b>user ID</b> (they can get it from @userinfobot).\n"
        "Note: they must have started this bot at least once. /cancel to abort.",
        parse_mode="HTML",
    )


@router.message(AddAdmin.waiting_user, F.text)
async def admin_user_received(message: types.Message, state: FSMContext, ctx: AppContext, shop_id: str, bot: Bot):
    if message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return
    raw = message.text.strip().lstrip("@")
    if not raw.isdigit():
        await message.answer("❌ Please send a numeric user ID (usernames can't be resolved reliably by bots).")
        return
    user_id = int(raw)
    await state.clear()

    username = None
    try:
        chat = await bot.get_chat(user_id)
        if chat.type != "private":
            await message.answer("❌ That ID doesn't belong to a user.")
            return
        username = chat.username
    except Exception:
        await message.answer("❌ I can't reach that user — they must /start this bot first.")
        return

    async with ctx.db() as session:
        try:
            session.add(StoreAdmin(shop_id=shop_id, user_id=user_id, username=username, admin_type="shop_admin"))
            await session.commit()
            await message.answer(f"✅ Admin added: {'@' + username if username else user_id}")
        except IntegrityError:
            await session.rollback()
            await message.answer("❌ That user is already an admin.")


@router.callback_query(F.data.startswith("adm_deladmin_"))
async def delete_admin(query: types.CallbackQuery, ctx: AppContext, shop_id: str):
    await query.answer()
    if not await _require_admin(ctx, shop_id, query.from_user.id):
        return
    try:
        user_id = int(query.data[len("adm_deladmin_"):])
    except ValueError:
        return
    async with ctx.db() as session:
        admin = await session.get(StoreAdmin, (shop_id, user_id))
        if not admin:
            return
        if admin.admin_type == "owner":
            await query.message.answer("❌ The owner can't be removed.")
            return
        await session.delete(admin)
        await session.commit()
    await query.message.answer(f"🗑 Admin {user_id} removed.")


# -------------------------------------------------------------------- orders

@router.callback_query(F.data == "adm_orders")
async def list_orders(query: types.CallbackQuery, ctx: AppContext, shop_id: str):
    await query.answer()
    if not await _require_admin(ctx, shop_id, query.from_user.id):
        return
    async with ctx.db() as session:
        orders = await ctx.orders.list_orders_for_shop(session, shop_id)
    if not orders:
        await query.message.answer("🧾 No orders yet.")
        return
    icons = {"pending": "🕓", "awaiting_verification": "🧾", "paid": "✅", "fulfilled": "📦", "cancelled": "❌"}
    lines = [
        f"{icons.get(o.status, '•')} #{o.id} — {o.product_title} — {o.amount} {o.currency} — {o.status}"
        + (f" (ref {o.payment_reference})" if o.payment_reference else "")
        for o in orders
    ]
    await query.message.answer("🧾 <b>Recent orders</b>\n\n" + "\n".join(lines), parse_mode="HTML")


@router.callback_query(F.data.startswith("ordpaid_"))
async def order_mark_paid(query: types.CallbackQuery, ctx: AppContext, shop_id: str, bot: Bot):
    await query.answer()
    if not await _require_admin(ctx, shop_id, query.from_user.id):
        return
    try:
        order_id = int(query.data[len("ordpaid_"):])
    except ValueError:
        return
    async with ctx.db() as session:
        order = await ctx.orders.set_status(session, order_id, "paid")
    if not order:
        await query.message.answer("❌ Order not found.")
        return
    await query.message.answer(f"✅ Order #{order.id} marked as paid.")
    try:
        await bot.send_message(order.customer_id, f"✅ Your payment for order #{order.id} ({order.product_title}) is confirmed. Thank you!")
    except Exception:
        pass


@router.callback_query(F.data.startswith("ordcancel_"))
async def order_cancel(query: types.CallbackQuery, ctx: AppContext, shop_id: str, bot: Bot):
    await query.answer()
    if not await _require_admin(ctx, shop_id, query.from_user.id):
        return
    try:
        order_id = int(query.data[len("ordcancel_"):])
    except ValueError:
        return
    async with ctx.db() as session:
        order = await ctx.orders.set_status(session, order_id, "cancelled")
    if not order:
        await query.message.answer("❌ Order not found.")
        return
    await query.message.answer(f"❌ Order #{order.id} cancelled.")
    try:
        await bot.send_message(order.customer_id, f"❌ Order #{order.id} ({order.product_title}) was cancelled by the shop.")
    except Exception:
        pass


# ---------------------------------------------------------- payment settings

@router.callback_query(F.data == "adm_paysettings")
async def payment_settings(query: types.CallbackQuery, state: FSMContext, ctx: AppContext, shop_id: str):
    await query.answer()
    if not await _require_admin(ctx, shop_id, query.from_user.id):
        return
    async with ctx.db() as session:
        _, instructions = await ctx.orders.get_shop_currency_and_instructions(session, shop_id)
    current = instructions or "(not set)"
    await state.set_state(PaymentSettings.waiting_text)
    await query.message.answer(
        "💳 <b>Payment instructions</b> shown to customers when they order:\n\n"
        f"<i>{current}</i>\n\n"
        "Send the new text (e.g. your CBE / Telebirr / Dashen account details), "
        "or /cancel to keep the current one.",
        parse_mode="HTML",
    )


@router.message(PaymentSettings.waiting_text, F.text)
async def payment_settings_received(message: types.Message, state: FSMContext, ctx: AppContext, shop_id: str):
    await state.clear()
    if message.text.strip() == "/cancel":
        await message.answer("Kept the current instructions.")
        return
    async with ctx.db() as session:
        settings = await session.get(ShopSettings, shop_id)
        if not settings:
            settings = ShopSettings(shop_id=shop_id)
            session.add(settings)
        settings.payment_instructions = message.text.strip()[:1000]
        await session.commit()
    await message.answer("✅ Payment instructions updated.")
