"""
享客虾 — 主程序 v0.5
个人AI秘书 · 微信原生 · 订阅制 · 微信支付
"""
import os
import asyncio
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from app.models import init_db, AsyncSessionLocal, Plan
from app.routes.admin import router as admin_router
from app.routes.wechat import router as wechat_router
from app.routes.public import router as public_router
from app.routes.pay import router as pay_router
from app.routes.auth import router as auth_router
from app.routes.menu import router as menu_router
from app.routes.upload import router as upload_router
from app.routes.generate import router as generate_router
from app.routes.documents import router as documents_router
from app.routes.analytics import router as analytics_router
from app.routes.bot_gateway import router as bot_gateway_router
from app.scheduler import start_scheduler
from app.bot.qqbot import run_qq_bot


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[享客虾] 连接数据库...")
    await init_db()
    print("[享客虾] 数据库就绪")

    async with AsyncSessionLocal() as session:
        from sqlalchemy import select, func
        result = await session.execute(select(func.count(Plan.id)))
        if result.scalar() == 0:
            from app.models import SEED_PLANS
            for p in SEED_PLANS:
                session.add(Plan(**p))
            await session.commit()
            print(f"[享客虾] 已填充 {len(SEED_PLANS)} 个套餐")

    start_scheduler()

    # 启动 QQ Bot（后台任务，不影响微信主服务）
    if os.getenv('QQ_BOT_APPID'):
        asyncio.create_task(run_qq_bot())
        print('[享客虾] QQ Bot 后台任务已启动')

    yield
    print("[享客虾] 服务停止")


app = FastAPI(
    title="享客虾 · AI秘书",
    version="0.5.2",
    description="微信里的私人AI秘书 · 享客虾，虾客行",
    lifespan=lifespan,
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

app.include_router(auth_router)
app.include_router(pay_router)
app.include_router(public_router)
app.include_router(admin_router, prefix='/api/admin')
app.include_router(wechat_router, prefix='/api/wechat')
app.include_router(menu_router)
app.include_router(upload_router)
app.include_router(generate_router)
app.include_router(documents_router, prefix='/api')
app.include_router(analytics_router, prefix='/api/analytics')
app.include_router(bot_gateway_router)  # 前缀已在 router 中定义
app.mount('/static', StaticFiles(directory='app/static'), name='static')
app.mount('/agents', StaticFiles(directory='/home/ubuntu/weclaw-1/agents'), name='agents')  # v0.5.0: 用户文件服务


@app.get('/', response_class=HTMLResponse)
async def landing():
    with open('app/templates/landing.html', 'r', encoding='utf-8') as f:
        return f.read()


@app.get('/admin', response_class=HTMLResponse)
async def admin_page():
    with open('app/templates/admin.html', 'r', encoding='utf-8') as f:
        return f.read()


@app.get('/api/health')
async def health():
    return {'status': 'ok', 'version': '0.5.2', 'service': '享客虾 · AI秘书'}
