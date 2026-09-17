from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from . import models  # noqa: F401  (import populates SQLModel.metadata before create_all)
from .config import settings

engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

_BACKEND_DIR = Path(__file__).resolve().parents[1]


def _alembic_bootstrap(fresh: bool) -> None:
    """Alembic is the single owner of schema changes (the earlier additive
    auto-ALTER helper is gone — two mechanisms fight over the first migration).

    fresh DB  : create_all already built the full current schema → ensure the
                version table exists and stamp head (revisions never re-run).
    existing  : upgrade head applies whatever deltas this code version carries.
                Never destructive — the data volume is never wiped.
    Runs in a thread: the async env.py calls asyncio.run(), which must not
    happen on the running event loop."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    if fresh:
        command.ensure_version(cfg)
        command.stamp(cfg, "head")
    else:
        command.upgrade(cfg, "head")


async def init_db() -> None:
    async with engine.begin() as conn:
        fresh = not await conn.run_sync(lambda c: inspect(c).has_table("card"))
        if fresh:
            await conn.run_sync(SQLModel.metadata.create_all)
    await asyncio.to_thread(_alembic_bootstrap, fresh)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_maker() as session:
        yield session
