"""Telegram Stars (XTR) provider — the default for subscription payments."""
import logging

from aiogram import Bot
from aiogram.types import LabeledPrice

from payments.base import PaymentProvider

logger = logging.getLogger(__name__)


class StarsProvider(PaymentProvider):
    code = "stars"
    display_name = "Telegram Stars"
    currency = "XTR"

    async def request_payment(
        self, bot: Bot, chat_id: int, *,
        invoice_id: str, amount: int, title: str, description: str,
    ) -> None:
        # Stars invoices need no provider_token; currency must be XTR and
        # prices a single LabeledPrice whose amount is the Star count.
        await bot.send_invoice(
            chat_id=chat_id,
            title=title,
            description=description,
            payload=invoice_id,
            currency="XTR",
            prices=[LabeledPrice(label=title, amount=amount)],
        )
        logger.info("[STARS] invoice %s (%s XTR) sent to %s", invoice_id, amount, chat_id)
