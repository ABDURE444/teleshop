from sqlalchemy import BigInteger, Integer
from sqlalchemy.orm import declarative_base

Base = declarative_base()

# BigInteger autoincrement PKs work on Postgres but not SQLite; this variant
# keeps production semantics while letting dev/tests run on SQLite.
BigIntPK = BigInteger().with_variant(Integer, "sqlite")
