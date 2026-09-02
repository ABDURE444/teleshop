"""Manual bank/wallet transfer provider (CBE, Telebirr, Dashen, ...).

Flow: the customer receives the shop's payment instructions, pays outside
Telegram, and submits a transaction reference (or screenshot). Verification
is delegated to a pluggable async `verifier` callable:

    async def verifier(reference: str) -> VerificationResult: ...

This is the integration point for the self-hosted verification engine
(receipt OCR + dedup + receiver-match + freshness checks). Until a verifier
is attached, every submission is marked `pending` and routed to shop admins
for manual confirmation — the system stays fully usable without it.
"""
import logging
from typing import Awaitable, Callable, Optional

from aiogram import Bot

from payments.base import PaymentProvider, VerificationResult

logger = logging.getLogger(__name__)

Verifier = Callable[[str], Awaitable[VerificationResult]]


class ManualTransferProvider(PaymentProvider):
    code = "manual"
    display_name = "Bank / Mobile money transfer"
    currency = "ETB"

    def __init__(self, verifier: Optional[Verifier] = None):
        self.verifier = verifier

    async def request_payment(
        self, bot: Bot, chat_id: int, *,
        invoice_id: str, amount: int, title: str, description: str,
    ) -> None:
        await bot.send_message(
            chat_id,
            f"🧾 <b>{title}</b>\n\n{description}\n\n"
            f"Amount: <b>{amount}</b>\n"
            f"Order reference: <code>{invoice_id}</code>\n\n"
            "After paying, reply with your transaction reference number "
            "(or send a screenshot of the receipt).",
            parse_mode="HTML",
        )

    async def verify(self, reference: str) -> VerificationResult:
        if self.verifier is None:
            # No engine attached yet -> human-in-the-loop path.
            return VerificationResult(
                verified=False, pending=True, reference=reference,
                reason="Awaiting admin confirmation (no auto-verifier configured)",
            )
        try:
            return await self.verifier(reference)
        except Exception as e:
            logger.exception("[MANUAL] verifier raised for reference %s", reference)
            return VerificationResult(verified=False, pending=True, reference=reference,
                                      reason=f"Verifier error: {e}")
