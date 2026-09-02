"""Database bootstrap: create tables and apply lightweight in-place upgrades.

Fresh installs get the full schema from `Base.metadata.create_all`. Existing
databases are upgraded by introspecting the live columns and adding only what
is missing, which works identically on PostgreSQL and SQLite — SQLite has no
`ADD COLUMN IF NOT EXISTS`, so the previous string-based approach silently
skipped every upgrade there and filled the log with syntax errors.
"""
import logging

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from models import Base

logger = logging.getLogger(__name__)

# table -> {column: DDL type}. Adding a column here is the whole migration.
COLUMN_UPGRADES: dict[str, dict[str, str]] = {
    "shop_settings": {
        "payment_instructions": "TEXT",
        "bank_name": "TEXT",
        "account_name": "TEXT",
        "account_number": "TEXT",
        "pickup_address": "TEXT",
        "topup_timeout_minutes": "INTEGER DEFAULT 60",
    },
    "orders": {
        "customer_name": "TEXT",
        "customer_phone": "TEXT",
        "pickup_time": "TEXT",
        "pickup_code": "VARCHAR(8)",
        "paid_total": "INTEGER DEFAULT 0",
        "refund_owed": "INTEGER DEFAULT 0",
        "collected_at": "TIMESTAMP",
    },
    "products": {
        "available": "INTEGER DEFAULT 1",
    },
    "shops": {
        "is_trial": "INTEGER DEFAULT 0",
        "trial_used": "INTEGER DEFAULT 0",
    },
}

# Columns retired in v2 (shop bots are asyncio tasks now, not OS processes).
COLUMN_DROPS: dict[str, list[str]] = {
    "shops": ["pid"],
}


class DatabaseService:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def initialize_database(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with self.engine.begin() as conn:
            existing = await conn.run_sync(self._inspect_columns)
            added = dropped = 0

            for table, columns in COLUMN_UPGRADES.items():
                if table not in existing:
                    continue
                for column, ddl in columns.items():
                    if column in existing[table]:
                        continue
                    try:
                        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                        added += 1
                        logger.info("[DB] added %s.%s", table, column)
                    except Exception as e:
                        logger.warning("[DB] could not add %s.%s: %s", table, column, e)

            # DROP COLUMN needs SQLite 3.35+; failure here is never fatal.
            for table, columns in COLUMN_DROPS.items():
                if table not in existing:
                    continue
                for column in columns:
                    if column not in existing[table]:
                        continue
                    try:
                        await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
                        dropped += 1
                        logger.info("[DB] dropped %s.%s", table, column)
                    except Exception as e:
                        logger.warning("[DB] could not drop %s.%s: %s", table, column, e)

        if added or dropped:
            logger.info("[DB] schema upgraded (+%s column(s), -%s)", added, dropped)
        logger.info("Database initialized (v2.1 schema)")

    @staticmethod
    def _inspect_columns(sync_conn) -> dict[str, set[str]]:
        inspector = inspect(sync_conn)
        return {t: {c["name"] for c in inspector.get_columns(t)}
                for t in inspector.get_table_names()}
