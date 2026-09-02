"""Master bot: shop creation (FSM), listing, viewing, toggling, deleting."""
import logging
import uuid

from aiogram import Bot, F, Router, types
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from context import AppContext
from master.states import CreateShop
from services.shop_service import utcnow

logger = logging.getLogger(__name__)
router = Router(name="master.shops")


def _shop_card_text(shop, running: bool) -> str:
    status = "🟢 Active" if shop.subscription_active == 1 else "🔴 Inactive"
    bot_state = "🟢 running" if running else "⚪ stopped"
    end = shop.subscription_end.date() if shop.subscription_end else "—"
    return (
        f"🏪 <b>{shop.name}</b>\n"
        f"🤖 Bot: @{(shop.username or '').lstrip('@') or '—'} ({bot_state})\n"
        f"📅 Subscription: {status}, expires {end}\n"
        f"🆔 <code>{shop.shop_id}</code>"
    )


def _shop_card_keyboard(ctx: AppContext, shop, user_id: int) -> InlineKeyboardMarkup:
    rows = []
    if shop.subscription_active != 1:
        rows.append([InlineKeyboardButton(text="💳 Subscribe & Activate", callback_data=f"subscribe_{shop.shop_id}")])
    else:
        running = ctx.bot_manager.is_running(shop.shop_id)
        label = "⏸ Stop Bot" if running else "▶️ Start Bot"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"toggle_{shop.shop_id}")])
    rows.append([InlineKeyboardButton(text="🔗 My Affiliate Link", callback_data=f"afflink_{shop.shop_id}")])
    rows.append([InlineKeyboardButton(text="🗑 Delete Shop", callback_data=f"delete_{shop.shop_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="my_shops")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _is_owner_or_super(ctx: AppContext, shop, user_id: int) -> bool:
    return user_id == shop.admin_id or user_id == ctx.config.super_admin_id


# ------------------------------------------------------------------ creation

@router.callback_query(F.data == "create_shop")
async def create_shop(query: types.CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(CreateShop.waiting_name)
    await query.message.answer("🏪 What should your shop be called?\n\n(Send the name as a message, or /cancel)")


@router.message(CreateShop.waiting_name)
async def shop_name_received(message: types.Message, state: FSMContext, ctx: AppContext):
    if message.text and message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return
    name = (message.text or "").strip()
    if not name or len(name) > 64:
        await message.answer("Please send a shop name up to 64 characters.")
        return
    async with ctx.db() as session:
        if await ctx.shops.check_shop_name_exists(session, message.from_user.id, name):
            await message.answer("❌ You already have a shop with that name. Choose another one.")
            return
    await state.update_data(shop_name=name)
    await state.set_state(CreateShop.waiting_token)
    await message.answer(
        f"✅ Name saved: <b>{name}</b>\n\n"
        "Now create a bot at @BotFather (/newbot) and paste its <b>token</b> here.\n"
        "It looks like <code>123456789:AAF...xyz</code>",
        parse_mode="HTML",
    )


@router.message(CreateShop.waiting_token)
async def shop_token_received(message: types.Message, state: FSMContext, ctx: AppContext):
    if message.text and message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return
    token = (message.text or "").strip()

    # Delete the user's message so the token doesn't linger in chat history.
    try:
        await message.delete()
    except Exception:
        pass

    if not ctx.shops.validate_token_format(token):
        await message.answer("❌ That doesn't look like a valid bot token. Please paste the token exactly as @BotFather sent it.")
        return

    async with ctx.db() as session:
        if await ctx.shops.check_token_already_used(session, token):
            await message.answer("❌ That token is already used by another shop. Each shop needs its own bot.")
            return

    # Validate against Telegram and auto-detect the bot username.
    probe = Bot(token=token)
    try:
        me = await probe.get_me()
        bot_username = me.username or ""
    except TelegramUnauthorizedError:
        await message.answer("❌ Telegram rejected this token. Double-check it at @BotFather.")
        return
    except Exception as e:
        await message.answer(f"❌ Could not verify the token with Telegram: {e}")
        return
    finally:
        try:
            await probe.session.close()
        except Exception:
            pass

    data = await state.get_data()
    name = data.get("shop_name", "My Shop")
    await state.clear()

    user_id = message.from_user.id
    shop_id = uuid.uuid4().hex[:12]

    referral = await ctx.cache.get_pending_referral(user_id)
    affiliate_id = referral["affiliate_id"] if referral else None
    affiliate_shop_id = referral["shop_id"] if referral else None

    async with ctx.db() as session:
        shop = await ctx.shops.create_shop(
            session,
            shop_id=shop_id, name=name, admin_id=user_id,
            token=token, username=bot_username,
            affiliate_id=affiliate_id, affiliate_shop_id=affiliate_shop_id,
            currency=ctx.config.default_currency,
        )
        if affiliate_id and affiliate_shop_id:
            await ctx.affiliates.create_or_get_affiliate(session, affiliate_id, affiliate_shop_id)
            await ctx.cache.delete_pending_referral(user_id)

    # --- free trial: the shop goes live immediately, no payment first.
    # This is what makes self-serve signup work — the owner can add products
    # and take a real order within minutes of creating the bot.
    trial_days = ctx.config.trial_days
    trial_started = False
    if trial_days > 0:
        async with ctx.db() as session:
            trial_started = bool(await ctx.shops.start_trial(session, shop_id, trial_days))
        if trial_started:
            ok, info = await ctx.bot_manager.start_shop(shop_id, token)
            if not ok:
                logger.error("[SHOP] trial start: could not launch bot for %s: %s", shop_id, info)
                trial_started = False

    if trial_started:
        await message.answer(
            f"🎉 Shop <b>{name}</b> is live!\n"
            f"🤖 Bot: @{bot_username}\n\n"
            f"Your free trial runs for <b>{trial_days} days</b> — no payment needed yet.\n\n"
            f"👉 Add your products here:\n{ctx.config.web_base_url}\n\n"
            f"When the trial ends, subscribe for {ctx.config.subscription_price_stars} ⭐ a year.",
            parse_mode="HTML", disable_web_page_preview=False,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🖥 Open dashboard", url=ctx.config.web_base_url)],
                [InlineKeyboardButton(text="⬅️ Main menu", callback_data="back_to_menu")],
            ]),
        )
    else:
        await message.answer(
            f"🎉 Shop <b>{name}</b> created!\n"
            f"🤖 Bot: @{bot_username}\n\n"
            f"Activate it with a yearly subscription ({ctx.config.subscription_price_stars} ⭐) to go live.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Subscribe & Activate", callback_data=f"subscribe_{shop_id}")],
                [InlineKeyboardButton(text="⬅️ Main menu", callback_data="back_to_menu")],
            ]),
        )


# ------------------------------------------------------------------- listing

@router.callback_query(F.data == "my_shops")
async def my_shops(query: types.CallbackQuery, ctx: AppContext):
    await query.answer()
    async with ctx.db() as session:
        shops = await ctx.shops.get_shops_by_admin(session, query.from_user.id)
    if not shops:
        await query.message.edit_text(
            "You don't have any shops yet.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏪 Create Shop", callback_data="create_shop")],
                [InlineKeyboardButton(text="⬅️ Back", callback_data="back_to_menu")],
            ]),
        )
        return
    rows = [
        [InlineKeyboardButton(
            text=f"{'🟢' if s.subscription_active == 1 else '🔴'} {s.name}",
            callback_data=f"view_{s.shop_id}",
        )] for s in shops
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="back_to_menu")])
    await query.message.edit_text("📋 Your shops:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "manage_all_shops")
async def manage_all_shops(query: types.CallbackQuery, ctx: AppContext):
    await query.answer()
    if query.from_user.id != ctx.config.super_admin_id:
        await query.message.answer("🚫 Admin access only")
        return
    async with ctx.db() as session:
        shops = await ctx.shops.get_all_shops(session)
    if not shops:
        await query.message.edit_text("No shops in the system yet.")
        return
    rows = [
        [InlineKeyboardButton(
            text=f"{'🟢' if s.subscription_active == 1 else '🔴'} {s.name} (owner {s.admin_id})",
            callback_data=f"view_{s.shop_id}",
        )] for s in shops[:50]
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="back_to_menu")])
    await query.message.edit_text(
        f"🛠 All shops ({len(shops)}):", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.callback_query(F.data.startswith("view_"))
async def view_shop(query: types.CallbackQuery, ctx: AppContext):
    await query.answer()
    shop_id = query.data[len("view_"):]
    async with ctx.db() as session:
        shop = await ctx.shops.get_shop(session, shop_id)
    if not shop:
        await query.message.answer("❌ Shop not found.")
        return
    if not _is_owner_or_super(ctx, shop, query.from_user.id):
        await query.message.answer("🚫 This isn't your shop.")
        return
    running = ctx.bot_manager.is_running(shop_id)
    await query.message.edit_text(
        _shop_card_text(shop, running),
        parse_mode="HTML",
        reply_markup=_shop_card_keyboard(ctx, shop, query.from_user.id),
    )


# ------------------------------------------------------------- toggle/delete

@router.callback_query(F.data.startswith("toggle_"))
async def toggle_shop(query: types.CallbackQuery, ctx: AppContext):
    await query.answer()
    shop_id = query.data[len("toggle_"):]
    async with ctx.db() as session:
        shop = await ctx.shops.get_shop(session, shop_id)
    if not shop:
        await query.message.answer("❌ Shop not found.")
        return
    if not _is_owner_or_super(ctx, shop, query.from_user.id):
        await query.message.answer("🚫 This isn't your shop.")
        return
    if shop.subscription_active != 1 or not shop.subscription_end or shop.subscription_end < utcnow():
        await query.message.answer("❌ This shop has no valid subscription. Subscribe first.")
        return

    if ctx.bot_manager.is_running(shop_id):
        await ctx.bot_manager.stop_shop(shop_id)
        await query.message.answer(f"⏸ Bot for <b>{shop.name}</b> stopped.", parse_mode="HTML")
    else:
        token = await ctx.cache.get_shop_token(shop_id) or shop.token
        ok, info = await ctx.bot_manager.start_shop(shop_id, token)
        if ok:
            await query.message.answer(f"▶️ Bot for <b>{shop.name}</b> is running: @{info}", parse_mode="HTML")
        else:
            await query.message.answer(f"❌ Could not start the bot ({info}). Check the token at @BotFather.")


@router.callback_query(F.data.startswith("delete_confirm_"))
async def delete_shop_confirmed(query: types.CallbackQuery, ctx: AppContext):
    await query.answer()
    shop_id = query.data[len("delete_confirm_"):]
    async with ctx.db() as session:
        shop = await ctx.shops.get_shop(session, shop_id)
        if not shop:
            await query.message.answer("❌ Shop not found.")
            return
        if not _is_owner_or_super(ctx, shop, query.from_user.id):
            await query.message.answer("🚫 This isn't your shop.")
            return
        name = shop.name
        await ctx.bot_manager.stop_shop(shop_id)
        ok = await ctx.shops.delete_shop_completely(session, shop_id)
    if ok:
        await query.message.edit_text(f"🗑 Shop <b>{name}</b> and all its data were deleted.", parse_mode="HTML")
    else:
        await query.message.answer("❌ Delete failed — check the logs.")


@router.callback_query(F.data.startswith("delete_"))
async def delete_shop_ask(query: types.CallbackQuery, ctx: AppContext):
    await query.answer()
    shop_id = query.data[len("delete_"):]
    await query.message.answer(
        "⚠️ This permanently deletes the shop, its products, orders and payment history. Are you sure?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Yes, delete everything", callback_data=f"delete_confirm_{shop_id}")],
            [InlineKeyboardButton(text="↩️ Keep the shop", callback_data=f"view_{shop_id}")],
        ]),
    )
