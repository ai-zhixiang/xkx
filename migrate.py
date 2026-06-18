"""
user_memories_v2 表迁移脚本
Usage: python3 migrate.py
"""
import asyncio, os
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://lucky:lucky_pass@localhost:5432/weclaw")

async def migrate():
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        # bot_session_messages_v2
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_session_messages_v2 (
                id BIGSERIAL PRIMARY KEY,
                session_id VARCHAR(128) NOT NULL,
                role VARCHAR(16) NOT NULL,
                content TEXT NOT NULL,
                tool_calls JSONB,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_v2_session_time
            ON bot_session_messages_v2(session_id, created_at)
        """)

        # user_memories_v2
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_memories_v2 (
                id BIGSERIAL PRIMARY KEY,
                user_id VARCHAR(128) NOT NULL UNIQUE,
                preferences JSONB DEFAULT '{}',
                summary TEXT,
                facts JSONB,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_v2_user_memories
            ON user_memories_v2(user_id)
        """)
    await engine.dispose()
    print("✅ 迁移完成")

if __name__ == "__main__":
    asyncio.run(migrate())
