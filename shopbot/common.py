"""Shared helpers for shop-bot handlers."""
import json

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Category, Product, StoreAdmin

PRODUCTS_PER_PAGE = 9


async def is_shop_admin(session: AsyncSession, shop_id: str, user_id: int, super_admin_id: int) -> bool:
    if user_id == super_admin_id:
        return True
    result = await session.execute(
        select(StoreAdmin.user_id).where(StoreAdmin.shop_id == shop_id, StoreAdmin.user_id == user_id)
    )
    return result.scalar_one_or_none() is not None


async def load_categories(session: AsyncSession, shop_id: str) -> list[Category]:
    result = await session.execute(
        select(Category).where(Category.shop_id == shop_id).order_by(Category.name)
    )
    return list(result.scalars().all())


async def count_products(session: AsyncSession, category_id: int) -> int:
    result = await session.execute(
        select(func.count(Product.id)).where(Product.category_id == category_id)
    )
    return result.scalar() or 0


async def load_products_page(session: AsyncSession, category_id: int, page: int) -> list[Product]:
    result = await session.execute(
        select(Product)
        .where(Product.category_id == category_id)
        .order_by(Product.title)
        .offset((page - 1) * PRODUCTS_PER_PAGE)
        .limit(PRODUCTS_PER_PAGE)
    )
    return list(result.scalars().all())


def media_ids(product: Product) -> list[str]:
    if not product.media_file_ids:
        return []
    try:
        parsed = json.loads(product.media_file_ids)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def parse_product_info(text: str) -> tuple[str, int, str, str] | str:
    """Parse 'Name | Price | Description | Links'. Returns tuple or error str."""
    parts = [p.strip() for p in (text or "").split("|")]
    if len(parts) < 2:
        return "Format: Name | Price | Description | Links (description and links optional)"
    title = parts[0]
    if not title:
        return "Product name can't be empty."
    try:
        price = int(parts[1].replace(",", "").strip())
    except ValueError:
        return "Price must be a whole number, e.g. 2500"
    description = parts[2] if len(parts) > 2 else ""
    links = parts[3] if len(parts) > 3 else ""
    return title, price, description, links
