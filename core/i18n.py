"""Tiny i18n layer.

Locales live in /locales/<code>.json (flat key -> string with {placeholders}).
A user's language is stored in Redis (ts:lang:<user_id>) once chosen; before
that it is auto-detected from Telegram's language_code. English is the
fallback for any missing key, so an incomplete locale never breaks a flow.
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED = ["en", "am", "ar", "ru", "uz"]
_FALLBACK = "en"
_LOCALES: dict[str, dict[str, str]] = {}

_locale_dir = Path(__file__).resolve().parent.parent / "locales"
for code in SUPPORTED:
    path = _locale_dir / f"{code}.json"
    try:
        _LOCALES[code] = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # missing/broken file must never kill startup
        logger.error("Could not load locale %s: %s", code, e)
        _LOCALES[code] = {}

LANG_NAMES = {
    "en": "English",
    "am": "አማርኛ",
    "ar": "العربية",
    "ru": "Русский",
    "uz": "O'zbekcha",
}


def norm_lang(code: str | None) -> str:
    """Map a Telegram language_code to a supported locale."""
    if not code:
        return _FALLBACK
    code = code.lower().split("-")[0]
    return code if code in SUPPORTED else _FALLBACK


def t(key: str, lang: str, **kwargs) -> str:
    template = _LOCALES.get(lang, {}).get(key) or _LOCALES[_FALLBACK].get(key) or key
    try:
        return template.format(**kwargs)
    except Exception:
        return template


async def get_lang(redis, user_id: int, tg_code: str | None = None) -> str:
    stored = await redis.get(f"ts:lang:{user_id}")
    if stored and stored in SUPPORTED:
        return stored
    return norm_lang(tg_code)


async def set_lang(redis, user_id: int, lang: str) -> None:
    if lang in SUPPORTED:
        await redis.set(f"ts:lang:{user_id}", lang)
