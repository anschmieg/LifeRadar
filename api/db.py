"""
Database connection using asyncpg.
"""
import asyncpg
import os
import traceback

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        host = os.environ.get("LIFE_RADAR_DB_HOST", "localhost")
        port = int(os.environ.get("LIFE_RADAR_DB_PORT", "5432"))
        user = os.environ.get("LIFE_RADAR_DB_USER", "liferadar")
        password = os.environ.get("LIFE_RADAR_DB_PASSWORD", "")
        database = os.environ.get("LIFE_RADAR_DB_NAME", "liferadar")

        print(f"[DB] Attempting to connect to {host}:{port} (user: {user}, db: {database})")
        
        try:
            _pool = await asyncpg.create_pool(
                host=host,
                port=port,
                user=user,
                password=password,
                database=database,
                min_size=2,
                max_size=10,
                timeout=10,
            )
            print(f"[DB] Pool created successfully!")
        except Exception as e:
            print(f"[DB] Failed to create pool: {e}")
            print(f"[DB] Traceback: {traceback.format_exc()}")
            raise
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def get_connection() -> asyncpg.Connection:
    pool = await get_pool()
    return await pool.acquire()


async def release_connection(conn: asyncpg.Connection):
    pool = await get_pool()
    await pool.release(conn)
