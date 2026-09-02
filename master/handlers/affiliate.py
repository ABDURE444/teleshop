"""Master bot: affiliate menu, referral link generation, earnings view."""
import logging

from aiogram import F, Router, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from context import AppContext

logger = logging.getLogger(__name__)
router = Router(name="master.affiliate")


@router.callback_query(F.data == "affiliate_menu")
async def affiliate_menu(query: types.CallbackQuery, ctx: AppContext):
    await query.answer()
    await query.message.edit_text(
        "🤝 <b>Affiliate program</b>\n\n"
        f"Share your referral link — every shop that subscribes through it earns you "
        f"<b>{ctx.config.affiliate_commission_credits} credits</b>.\n\n"
        f"Once you hold <b>{ctx.config.affiliate_payout_threshold}+ credits</b>, buyers you refer "
        "pay through <i>your</i> shop bot and you receive the Stars directly.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Get my referral link", callback_data="affiliate_pick_shop")],
            [InlineKeyboardButton(text="📊 My earnings", callback_data="view_affiliate_earnings")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="back_to_menu")],
        ]),
    )


@router.callback_query(F.data == "affiliate_pick_shop")
async def affiliate_pick_shop(query: types.CallbackQuery, ctx: AppContext):
    await query.answer()
    async with ctx.db() as session:
        shops = await ctx.shops.get_shops_by_admin(session, query.from_user.id)
    if not shops:
        await query.message.answer(
            "You need a shop of your own before you can refer others — "
            "commissions are tracked per shop.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏪 Create Shop", callback_data="create_shop")]
            ]),
        )
        return
    rows = [
        [InlineKeyboardButton(text=s.name, callback_data=f"afflink_{s.shop_id}")]
        for s in shops
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="affiliate_menu")])
    await query.message.edit_text(
        "Which shop should this referral link belong to?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("afflink_"))
async def generate_affiliate_link(query: types.CallbackQuery, ctx: AppContext):
    await query.answer()
    shop_id = query.data[len("afflink_"):]
    user_id = query.from_user.id

    async with ctx.db() as session:
        shop = await ctx.shops.get_shop(session, shop_id)
        if not shop or shop.admin_id != user_id:
            await query.message.answer("🚫 You can only generate links for your own shops.")
            return
        await ctx.affiliates.create_or_get_affiliate(session, user_id, shop_id)

    payload = ctx.affiliates.build_referral_payload(user_id, shop_id)
    link = f"https://t.me/{ctx.master_bot_username}?start={payload}"
    await query.message.answer(
        "🔗 <b>Your referral link</b>\n\n"
        f"<code>{link}</code>\n\n"
        f"Every shop created through this link that subscribes earns you "
        f"{ctx.config.affiliate_commission_credits} credits (tracked on <b>{shop.name}</b>).",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "view_affiliate_earnings")
async def view_affiliate_earnings(query: types.CallbackQuery, ctx: AppContext):
    await query.answer()
    async with ctx.db() as session:
        stats = await ctx.affiliates.get_total_stats(session, query.from_user.id)
    if not stats["records"]:
        await query.message.answer("📊 No affiliate activity yet. Generate a referral link and share it!")
        return
    lines = [
        f"• Shop <code>{r.shop_id}</code>: {r.credit_balance or 0} credits (lifetime {r.stars_earned or 0})"
        for r in stats["records"]
    ]
    threshold = ctx.config.affiliate_payout_threshold
    eligible = "✅ eligible for direct payments" if stats["total_credits"] >= threshold else f"({threshold - stats['total_credits']} credits to direct-payment eligibility)"
    await query.message.answer(
        "📊 <b>Affiliate earnings</b>\n\n" + "\n".join(lines) +
        f"\n\n💰 Total credits: <b>{stats['total_credits']}</b> {eligible}",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "master_earnings")
async def master_earnings(query: types.CallbackQuery, ctx: AppContext):
    await query.answer()
    if query.from_user.id != ctx.config.super_admin_id:
        await query.message.answer("🚫 Admin access only")
        return
    async with ctx.db() as session:
        total = await ctx.payments.get_master_earnings(session)
    await query.message.answer(f"💰 Master earnings (paid yearly subscriptions): <b>{total} ⭐</b>", parse_mode="HTML")
