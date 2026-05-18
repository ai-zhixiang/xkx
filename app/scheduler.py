"""
享客虾 — 定时任务
到期检测 / 续费提醒
"""
from datetime import date, datetime, timedelta

from sqlalchemy import select
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.models import (
    AsyncSessionLocal, Subscriber, SubscriberStatus
)

scheduler = AsyncIOScheduler()


async def check_expired_subscribers():
    """每天检查到期订阅 → 自动标记过期"""
    today = date.today()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Subscriber).where(
                Subscriber.status == SubscriberStatus.ACTIVE,
                Subscriber.expires_at <= today,
            )
        )
        expired = result.scalars().all()

        for sub in expired:
            sub.status = SubscriberStatus.EXPIRED
            print(f"[到期] 用户 #{sub.id} '{sub.nickname}' 已到期，自动关闭")

        if expired:
            await db.commit()
            print(f"[到期] 共 {len(expired)} 个用户订阅被关闭")
        else:
            print(f"[到期] 今日无到期用户")


async def check_expiring_soon():
    """检查3天内到期 → 日志（后续对接通知）"""
    today = date.today()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Subscriber).where(
                Subscriber.status == SubscriberStatus.ACTIVE,
                Subscriber.expires_at <= today + timedelta(days=3),
                Subscriber.expires_at > today,
            )
        )
        soon = result.scalars().all()

        if soon:
            print(f"[提醒] 以下用户3天内到期：")
            for s in soon:
                days = (s.expires_at - today).days
                print(f"   #{s.id} '{s.nickname}' — 还剩 {days} 天")
        else:
            print(f"[提醒] 今日无即将到期用户")


def start_scheduler():
    """启动定时任务"""
    scheduler.add_job(check_expired_subscribers, 'cron', hour=2, minute=0, id='check_expired')
    scheduler.add_job(check_expiring_soon, 'cron', hour=9, minute=0, id='check_expiring')
    scheduler.start()
    print("[定时] 到期检测 02:00 | 续费提醒 09:00")
