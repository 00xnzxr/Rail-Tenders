"""
Alembic migration environment.
Imports all models so autogenerate can detect schema changes.
"""

from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context

# Import Base and all models so autogenerate works
from app.core.database import Base
from app.core.config import get_settings
import app.models  # noqa: F401 — triggers model registration

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Use DATABASE_URL from app config instead of hardcoded alembic.ini value
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline():
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
