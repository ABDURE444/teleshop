"""Master bot: /start (with deep-link payloads), /help, main menu."""
import logging

from aiogram import F, Router, types
from aiogram.filters import CommandObject, CommandStart, Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from context import AppContext

logger = logging.getLogger(__name__)
router = Router(name="master.start")


def main_menu_keyboard(ctx: AppContext, user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🏪 Create Shop", callback_data="create_shop")],
        [InlineKeyboardButton(text="📋 My Shops", callback_data="my_shops")],
        [InlineKeyboardButton(text="🤝 Affiliate Program", callback_data="affiliate_menu")],
    ]
    if user_id == ctx.config.super_admin_id:
        rows.append([InlineKeyboardButton(text="🛠 Manage All Shops", callback_data="manage_all_shops")])
        rows.append([InlineKeyboardButton(text="💰 Master Earnings", callback_data="master_earnings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart(deep_link=True))
async def start_with_payload(message: types.Message, command: CommandObject, ctx: AppContext):
    payload = (command.args or "").strip()
    user_id = message.from_user.id

    # Affiliate referral link: affiliate_<id>_shop_<shop_id>
    parsed = ctx.affiliates.parse_referral_payload(payload)
    if parsed:
        affiliate_id, source_shop_id = parsed
        if affiliate_id != user_id:  # you can't refer yourself
            await ctx.cache.set_pending_referral(user_id, affiliate_id, source_shop_id)
            logger.info("[START] user %s arrived via affiliate %s (shop %s)", user_id, affiliate_id, source_shop_id)
            await message.answer(
                "👋 Welcome to <b>Teleshop</b>!\n\n"
                "You arrived through a partner link — create your shop now and "
                "your referrer will be credited automatically.",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard(ctx, user_id),
            )
            return

    # Subscription deep link: subscribe_<shop_id>
    if payload.startswith("subscribe_"):
        shop_id = payload[len("subscribe_"):]
        await message.answer(
            "💳 Ready to activate your shop?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Subscribe", callback_data=f"subscribe_{shop_id}")]
            ]),
        )
        return

    await start_plain(message, ctx)


@router.message(CommandStart())
async def start_plain(message: types.Message, ctx: AppContext):
    await message.answer(
        "👋 Welcome to <b>Teleshop</b> — create and run your own Telegram shop bot.\n\n"
        "🏪 Create a shop with its own bot\n"
        "📦 Manage categories, products and orders\n"
        "🤝 Earn by referring other merchants",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(ctx, message.from_user.id),
    )


@router.message(Command("help"))
async def help_command(message: types.Message, ctx: AppContext):
    await message.answer(
        "ℹ️ <b>How Teleshop works</b>\n\n"
        "1. Tap <b>Create Shop</b> and give it a name.\n"
        "2. Create a bot at @BotFather and paste its token here.\n"
        f"3. Pay the yearly subscription ({ctx.config.subscription_price_stars} ⭐) to activate.\n"
        "4. Your shop bot goes live instantly — manage products with /admin inside it.\n\n"
        "🤝 <b>Affiliate program</b>: share your referral link; every shop that "
        f"subscribes through it earns you {ctx.config.affiliate_commission_credits} credits.",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(query: types.CallbackQuery, ctx: AppContext):
    await query.answer()
    await query.message.edit_text(
        "🏠 Main menu",
        reply_markup=main_menu_keyboard(ctx, query.from_user.id),
    )
