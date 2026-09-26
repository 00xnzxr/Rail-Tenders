"""
DRPL Backend - Database Setup
SQLAlchemy 2.0 engine and session
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from app.core.config import get_settings

settings = get_settings()

_DEFAULT_POOL_SIZE = 10
_DEFAULT_MAX_OVERFLOW = 20

# Env-aware pool sizing — worker service sets these lower (DB_POOL_SIZE=5,
# DB_MAX_OVERFLOW=10) to avoid exhausting Neon connection budget when web
# and worker share the same database.
_pool_size = int(os.getenv("DB_POOL_SIZE", str(_DEFAULT_POOL_SIZE)))
_max_overflow = int(os.getenv("DB_MAX_OVERFLOW", str(_DEFAULT_MAX_OVERFLOW)))

# SQLite needs special handling
connect_args = {}
# hide_parameters: SQL echo (on whenever DEBUG is, which is the config
# default) printed every bound parameter -- chat turns, extracted document
# text, analysis JSON, password hashes -- into the platform logs, and so did
# every DB error message. The statements are still logged; the values are not.
engine_kwargs = {"echo": settings.debug, "hide_parameters": True}

_db_url = settings.database_url

if _db_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
else:
    engine_kwargs["pool_pre_ping"] = True
    engine_kwargs["pool_size"] = _pool_size
    engine_kwargs["max_overflow"] = _max_overflow
    engine_kwargs["pool_recycle"] = 60  # Recycle connections every 60s (Neon drops idle connections ~30s)
    # TCP keepalives keep a checked-out-but-idle connection alive at the kernel
    # level during long LLM calls. pool_pre_ping/pool_recycle only act at
    # checkout time — they cannot save a connection that was already in use
    # when Neon dropped it. Without these, a 5-min agent run will fail at the
    # first SQL after the LLM returns ("SSL connection has been closed
    # unexpectedly"). libpq forwards these to the OS-level socket.
    connect_args = {
        "connect_timeout": 60,
        "keepalives": 1,
        "keepalives_idle": 30,       # send first probe after 30s idle
        "keepalives_interval": 10,   # retry every 10s
        "keepalives_count": 3,       # drop after 3 failed retries (~60s total)
    }

    # Auto-detect Neon pooled endpoint (`*-pooler.*`) or explicit ?pgbouncer=true.
    # PgBouncer in transaction mode breaks prepared statements; psycopg3
    # needs prepare_threshold=None to disable its prepared-statement cache.
    _is_pgbouncer = (
        "?pgbouncer=true" in _db_url
        or "&pgbouncer=true" in _db_url
        or "-pooler." in _db_url
    )
    if _is_pgbouncer:
        # psycopg3 (the default driver we use) honors prepare_threshold
        connect_args["prepare_threshold"] = None

engine = create_engine(
    _db_url,
    connect_args=connect_args,
    **engine_kwargs,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency that provides a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        try:
            db.close()
        except Exception:
            pass  # Connection may have been dropped by Neon idle timeout — harmless
