from contextlib import asynccontextmanager

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

_pool: AsyncConnectionPool | None = None


async def open_pool(dsn: str) -> None:
    global _pool
    # prepare_threshold=None keeps this compatible with Supabase's transaction pooler.
    _pool = AsyncConnectionPool(
        dsn, min_size=1, max_size=5, open=False,
        kwargs={"row_factory": dict_row, "prepare_threshold": None},
    )
    await _pool.open()


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def conn():
    assert _pool is not None, "database pool not opened"
    async with _pool.connection() as c:
        yield c
