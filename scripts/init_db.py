"""Apply pending SQL migrations (migrations/*.sql in order).

Equivalent to init_db inside the FastAPI lifespan, split out for convenient script/CI use.
Usage: python scripts/init_db.py   # or: psql -f migrations/0001_init.sql for a single script
"""
import asyncio

from core.infrastructure.db import init_db


async def main() -> None:
    await init_db()
    print("✓ 数据库表已就绪(migrations/*.sql 已按序执行)")


if __name__ == "__main__":
    asyncio.run(main())
