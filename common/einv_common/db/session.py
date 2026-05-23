from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from einv_common.config import settings

engine = create_async_engine(
    settings.database_url,
    pool_size=settings.database_pool_min,
    max_overflow=settings.database_pool_max - settings.database_pool_min,
    pool_pre_ping=True,
    echo=settings.environment == "development",
)

session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_db() -> None:
    """Raises if the database is unreachable — used by /health/ready."""
    async with session_factory() as session:
        await session.execute(text("SELECT 1"))
