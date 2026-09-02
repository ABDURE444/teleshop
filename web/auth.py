"""Dashboard authentication via the Telegram Login Widget.

No passwords, no auth provider, no third-party service. Telegram signs the
login payload with HMAC-SHA256 keyed on SHA256(bot_token); we verify that
signature, then issue our own signed session cookie.

The widget requires the master bot's domain to be registered in @BotFather
(/setdomain) and it must match WEB_BASE_URL exactly.
"""
import hashlib
import hmac
import json
import logging
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode

from fastapi import Request

logger = logging.getLogger(__name__)

COOKIE_NAME = "ts_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
LOGIN_MAX_AGE = 86400               # reject Telegram payloads older than a day


def verify_telegram_login(data: dict, bot_token: str) -> dict | None:
    """Return the user dict if the Telegram Login Widget payload is authentic."""
    received_hash = data.get("hash")
    if not received_hash:
        return None
    pairs = sorted(f"{k}={v}" for k, v in data.items() if k != "hash")
    check_string = "\n".join(pairs)
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        logger.warning("[WEB] Telegram login signature mismatch")
        return None
    try:
        if time.time() - int(data.get("auth_date", 0)) > LOGIN_MAX_AGE:
            logger.warning("[WEB] Telegram login payload expired")
            return None
    except (TypeError, ValueError):
        return None
    return {
        "id": int(data["id"]),
        "first_name": data.get("first_name", ""),
        "username": data.get("username", ""),
    }


# ------------------------------------------------------------------ sessions

def _sign(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def make_session(user: dict, secret: str) -> str:
    body = json.dumps({"uid": user["id"], "name": user.get("first_name", ""),
                       "username": user.get("username", ""), "iat": int(time.time())},
                      separators=(",", ":")).encode()
    b64 = urlsafe_b64encode(body).decode().rstrip("=")
    return f"{b64}.{_sign(body, secret)}"


def read_session(token: str | None, secret: str) -> dict | None:
    if not token or "." not in token:
        return None
    b64, sig = token.rsplit(".", 1)
    try:
        body = urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
    except Exception:
        return None
    if not hmac.compare_digest(_sign(body, secret), sig):
        return None
    try:
        data = json.loads(body)
    except Exception:
        return None
    if time.time() - data.get("iat", 0) > SESSION_MAX_AGE:
        return None
    return data


def current_user(request: Request) -> dict | None:
    secret = request.app.state.ctx.config.web_session_secret
    return read_session(request.cookies.get(COOKIE_NAME), secret)
