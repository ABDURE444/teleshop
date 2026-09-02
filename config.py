"""Typed application configuration, loaded once from environment / .env."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class Config:
    # Core
    bot_token: str
    database_url: str
    redis_url: str
    super_admin_id: int

    # Subscription pricing (Telegram Stars, XTR)
    subscription_price_stars: int  # direct payment to master bot
    subscription_days: int

    # Affiliate system
    affiliate_commission_credits: int   # credits earned per referred shop
    affiliate_payout_threshold: int     # credit balance needed for direct payments
    affiliate_payment_amount: int       # Stars a buyer pays to an affiliate shop bot

    # Trial
    trial_days: int

    # Web dashboard
    web_base_url: str
    web_host: str
    web_port: int
    web_session_secret: str

    # Misc
    default_currency: str
    log_level: str
    log_dir: str

    @classmethod
    def load(cls) -> "Config":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        database_url = os.getenv("DATABASE_URL", "").strip()
        missing = [n for n, v in (("BOT_TOKEN", bot_token), ("DATABASE_URL", database_url)) if not v]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

        return cls(
            bot_token=bot_token,
            database_url=database_url,
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0").strip(),
            super_admin_id=_int_env("SUPER_ADMIN_ID", 0),
            subscription_price_stars=_int_env("SUBSCRIPTION_PRICE_STARS", 1200),
            subscription_days=_int_env("SUBSCRIPTION_DAYS", 365),
            affiliate_commission_credits=_int_env("AFFILIATE_COMMISSION_CREDITS", 300),
            affiliate_payout_threshold=_int_env("AFFILIATE_PAYOUT_THRESHOLD", 1300),
            affiliate_payment_amount=_int_env("AFFILIATE_PAYMENT_AMOUNT", 1300),
            trial_days=_int_env("TRIAL_DAYS", 30),
            web_base_url=os.getenv("WEB_BASE_URL", "http://localhost:8080").strip().rstrip("/"),
            web_host=os.getenv("WEB_HOST", "0.0.0.0").strip(),
            web_port=_int_env("WEB_PORT", 8080),
            web_session_secret=os.getenv("WEB_SESSION_SECRET", "change-me-in-production").strip(),
            default_currency=os.getenv("DEFAULT_CURRENCY", "ETB").strip(),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            log_dir=os.getenv("LOG_DIR", "logs").strip(),
        )
