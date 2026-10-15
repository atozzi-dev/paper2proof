"""Shared fixtures."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from app.storage.db import make_engine
from app.storage.store import Store

ROOT = Path(__file__).resolve().parents[1]


def alembic_config(db_path: Path) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    return cfg


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    """A Store on a fresh file DB migrated to head (WAL needs a real file)."""
    db_path = tmp_path / "p2p.db"
    command.upgrade(alembic_config(db_path), "head")
    engine = make_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    yield Store(engine)
    await engine.dispose()
