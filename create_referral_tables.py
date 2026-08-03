"""Create referral tables in weclawd database"""
import asyncio
import sys
sys.path.insert(0, '/home/ubuntu/weclaw-1')

from app.models import Base, init_db
from sqlalchemy import text


async def main():
    await init_db()
    from app.models import async_engine

    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    print("✅ Tables created!")

    async with async_engine.connect() as conn:
        for t in ['referral_codes', 'referral_relations', 'referral_commissions']:
            result = await conn.execute(
                text(f"SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name='{t}')")
            )
            exists = result.scalar()
            print(f"  {t}: {'✅' if exists else '❌'}")


asyncio.run(main())
