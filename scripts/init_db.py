"""Initialize the database from the canonical schema (migrations/0001_init.sql).

Equivalent to init_db inside the FastAPI lifespan, split out for convenient script/CI use.
Usage: python scripts/init_db.py   # or: psql -f migrations/0001_init.sql directly
"""
import asyncio

from core.infrastructure.db import init_db


async def main() -> None:
    await init_db()
    print("✓ 数据库已初始化(canonical schema 已应用)")


if __name__ == "__main__":
    asyncio.run(main())
