"""
Database connection using asyncpg.
"""
import asyncpg
import os

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        host = os.environ.get("LIFERADAR_DB_HOST", "localhost")
        port = int(os.environ.get("LIFERADAR_DB_PORT", "5432"))
        user = os.environ.get("LIFERADAR_DB_USER", "life_radar")
        password = os.environ.get("LIFERADAR_DB_PASSWORD", "")
        database = os.environ.get("LIFERADAR_DB_NAME", "life_radar")

        # Retry transient startup failures (DNS/race during compose start) so the
        # container stays up and /health can report the real error instead of
        # uvicorn crash-looping with no visible logs.
        import asyncio
        last_error: Exception | None = None
        for attempt in range(30):
            try:
                _pool = await asyncpg.create_pool(
                    host=host,
                    port=port,
                    user=user,
                    password=password,
                    database=database,
                    min_size=2,
                    max_size=10,
                )
                break
            except Exception as e:
                last_error = e
                await asyncio.sleep(2)
        else:
            raise RuntimeError(f"db connect failed after 30 attempts: {host}:{port} {last_error}")
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def get_connection() -> asyncpg.Connection:
    pool = await get_pool()
    return await pool.acquire()
