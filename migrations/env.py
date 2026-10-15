"""Alembic environment. Migrations run synchronously; the async driver is swapped for pysqlite."""

from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, make_url, pool

from app.settings import get_settings
from app.storage.tables import Base

config = context.config
target_metadata = Base.metadata


def _sync_url() -> str:
    url = config.get_main_option("sqlalchemy.url") or get_settings().db_url
    sync = url.replace("+aiosqlite", "")
    database = make_url(sync).database
    if sync.startswith("sqlite") and database and database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)  # e.g. ./var on a fresh checkout
    return sync


def run_migrations_offline() -> None:
    context.configure(url=_sync_url(), target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_sync_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
