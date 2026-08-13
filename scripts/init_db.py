"""Explicitly initialize the database: enable the pgvector extension + create tables.

Equivalent to init_db inside the FastAPI lifespan, split out for convenient script/CI use.
Usage: python scripts/init_db.py
"""
import asyncio

from core.infrastructure.db import init_db


async def main() -> None:
    await init_db()
    print("✓ 数据库扩展 + 表已就绪")


if __name__ == "__main__":
    asyncio.run(main())
