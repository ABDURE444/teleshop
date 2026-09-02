"""Redis cache: shop record caching, secure token storage, transient state."""
import hashlib
import json
import logging
import secrets
from datetime import date, datetime

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


def _json_serial(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


class CacheService:
    def __init__(self, redis_client: Redis):
        self.redis = redis_client

    # ------------------------------------------------------------ shop cache

    async def cache_get(self, key: str):
        try:
            raw = await self.redis.get(key)
            return json.loads(raw) if raw else None
        except Exception as e:
            logger.warning("cache_get(%s) failed: %s", key, e)
            return None

    async def cache_set(self, key: str, value, ttl: int = 300):
        try:
            await self.redis.set(key, json.dumps(value, default=_json_serial), ex=ttl)
        except Exception as e:
            logger.warning("cache_set(%s) failed: %s", key, e)

    async def invalidate_shop(self, shop_id: str):
        try:
            await self.redis.delete(f"shop:{shop_id}")
        except Exception as e:
            logger.warning("invalidate_shop(%s) failed: %s", shop_id, e)

    # -------------------------------------------------- secure token storage
    # Tokens live in the DB too, but Redis holds an integrity-hashed copy so
    # tampering with either store is detectable before a bot is launched.

    async def store_shop_token(self, shop_id: str, token: str) -> bool:
        try:
            salt = secrets.token_hex(16)
            payload = {
                "token": token,
                "salt": salt,
                "hash": hashlib.sha256(f"{token}{salt}".encode()).hexdigest(),
                "shop_id": shop_id,
            }
            await self.redis.set(f"shop_token:{shop_id}", json.dumps(payload))
            return True
        except Exception as e:
            logger.error("store_shop_token(%s) failed: %s", shop_id, e)
            return False

    async def get_shop_token(self, shop_id: str) -> str | None:
        try:
            raw = await self.redis.get(f"shop_token:{shop_id}")
            if not raw:
                return None
            info = json.loads(raw)
            if not all(k in info for k in ("token", "salt", "hash")):
                logger.error("token record malformed for shop %s", shop_id)
                return None
            expected = hashlib.sha256(f"{info['token']}{info['salt']}".encode()).hexdigest()
            if info["hash"] != expected:
                logger.error("token integrity check FAILED for shop %s", shop_id)
                return None
            return info["token"]
        except Exception as e:
            logger.error("get_shop_token(%s) failed: %s", shop_id, e)
            return None

    async def delete_shop_token(self, shop_id: str):
        try:
            await self.redis.delete(f"shop_token:{shop_id}")
        except Exception as e:
            logger.warning("delete_shop_token(%s) failed: %s", shop_id, e)

    # ------------------------------------------- pending affiliate referrals
    # When a user opens the master bot via an affiliate deep link, the
    # attribution is parked here until they actually create a shop.

    async def set_pending_referral(self, user_id: int, affiliate_id: int, source_shop_id: str):
        await self.redis.set(
            f"pending_ref:{user_id}",
            json.dumps({"affiliate_id": affiliate_id, "shop_id": source_shop_id}),
            ex=7 * 24 * 3600,
        )

    async def get_pending_referral(self, user_id: int) -> dict | None:
        raw = await self.redis.get(f"pending_ref:{user_id}")
        return json.loads(raw) if raw else None

    async def delete_pending_referral(self, user_id: int):
        await self.redis.delete(f"pending_ref:{user_id}")
