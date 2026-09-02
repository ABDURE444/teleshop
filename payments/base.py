"""Payment provider adapter interface.

Everything payment-related flows through this seam, so swapping or adding
providers (Telegram Stars today; CBE / Telebirr / Dashen verification
tomorrow) never touches handler or service code.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass

from aiogram import Bot


@dataclass
class VerificationResult:
    verified: bool
    pending: bool = False          # True = needs human/async verification
    reference: str | None = None
    reason: str | None = None


class PaymentProvider(ABC):
    code: str = "base"
    display_name: str = "Base provider"
    currency: str = "XTR"

    @abstractmethod
    async def request_payment(
        self, bot: Bot, chat_id: int, *,
        invoice_id: str, amount: int, title: str, description: str,
    ) -> None:
        """Ask the user to pay. For Stars this sends a Telegram invoice; for
        manual-transfer providers it sends instructions + a reference prompt."""

    async def verify(self, reference: str) -> VerificationResult:
        """Verify an out-of-band payment reference. Providers with real-time
        rails (Stars) never call this; manual-transfer providers override it."""
        return VerificationResult(verified=False, pending=True, reference=reference,
                                  reason="No automatic verification configured")


class ProviderRegistry:
    def __init__(self):
        self._providers: dict[str, PaymentProvider] = {}

    def register(self, provider: PaymentProvider) -> None:
        self._providers[provider.code] = provider

    def get(self, code: str) -> PaymentProvider | None:
        return self._providers.get(code)

    def all(self) -> list[PaymentProvider]:
        return list(self._providers.values())
