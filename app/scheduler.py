"""
小龙虾AI平台 — 定时任务
到期检测 / 续费提醒
"""
import os
from datetime import date, datetime, timedelta

from sqlalchemy import select
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.models import (
    AsyncSessionLocal, Tenant, TenantStatus
)

scheduler = AsyncIOScheduler()


async def check_expired_tenants():
    """每天检查到期租户 → 自动标记过期"""
    today = date.today()
    
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Tenant).where(
                Tenant.status == TenantStatus.ACTIVE,
                Tenant.expires_at <= today,
            )
        )
        expired = result.scalars().all()
        
        for tenant in expired:
            tenant.status = TenantStatus.EXPIRED
            print(f"[到期] 租户 #{tenant.id} '{tenant.name}' 已到期，自动关闭")
        
        if expired:
            await db.commit()
            print(f"[到期] 共 {len(expired)} 个租户被关闭")
        else:
            print(f"[到期] 今日无到期租户")


async def check_expiring_soon():
    """检查5天内到期 → 打印日志（后续对接通知）"""
    today = date.today()
    
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Tenant).where(
                Tenant.status == TenantStatus.ACTIVE,
                Tenant.expires_at <= today + timedelta(days=5),
                Tenant.expires_at > today,
            )
        )
        soon = result.scalars().all()
        
        if soon:
            print(f"[提醒] 以下租户5天内到期：")
            for t in soon:
                days = (t.expires_at - today).days
                print(f"   #{t.id} '{t.name}' — 还剩 {days} 天")


def start_scheduler():
    """启动定时任务"""
    scheduler.add_job(check_expired_tenants, 'cron', hour=2, minute=0, id='check_expired')
    scheduler.add_job(check_expiring_soon, 'cron', hour=9, minute=0, id='check_expiring')
    scheduler.start()
    print("[定时] 到期检测 02:00 | 续费提醒 09:00")
