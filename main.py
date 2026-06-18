"""
享客虾智能网关 v2 — 入口
"""
import os, logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("weclawd-v2")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://lucky:lucky_pass@localhost:5432/weclaw")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", "")

engine = create_async_engine(DATABASE_URL, pool_size=5, max_overflow=10)
async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

app = FastAPI(title="享客虾智能网关 v2")

async def get_db():
    async with async_session_factory() as db:
        yield db

@app.get("/health")
async def health():
    return {"status": "ok", "version": 2, "service": "weclawd-v2"}


@app.get("/v2/session/{session_id}")
async def get_session_stats(session_id: str):
    """查看 session 统计"""
    async with async_session_factory() as db:
        from sqlalchemy import text
        row = await db.execute(
            text("SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM bot_session_messages_v2 WHERE session_id=:sid"),
            {"sid": session_id},
        )
        r = row.fetchone()
        return {
            "session_id": session_id,
            "total_messages": r[0],
            "first_message": str(r[1]) if r[1] else None,
            "last_message": str(r[2]) if r[2] else None,
        }


@app.get("/v2/memory/{user_id}")
async def get_user_memory(user_id: str):
    """查看用户记忆"""
    async with async_session_factory() as db:
        from app.bot.context import load_user_memory
        memory = await load_user_memory(db, user_id)
    return {"user_id": user_id, "memory": memory or {}}
