"""
享客虾 — 公开接口 v0.1
套餐 + 订阅开通（无免费试用）
"""
import os
from datetime import date, timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.models import get_db, Plan, Subscriber, SubOrder, SubscriberStatus, OrderStatus

router = APIRouter()


@router.get('/api/plans')
async def list_plans(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Plan).where(Plan.is_active == True).order_by(Plan.sort_order)
    )
    plans = result.scalars().all()
    return [{
        'id': p.id, 'name': p.name, 'price': p.price,
        'price_yuan': f'{p.price/100:.1f}'.rstrip('0').rstrip('.'),
        'monthly_messages': p.monthly_messages,
        'messages_label': f'{p.monthly_messages}条/月' if p.monthly_messages else '不限量',
    } for p in plans]


@router.get('/api/me')
async def get_my_info(openid: str = '', db: AsyncSession = Depends(get_db)):
    if not openid:
        return {'subscribed': False}
    result = await db.execute(
        select(Subscriber).options(selectinload(Subscriber.plan))
        .where(Subscriber.openid == openid)
    )
    sub = result.scalar_one_or_none()
    if not sub or sub.status != SubscriberStatus.ACTIVE:
        return {'subscribed': False}
    today = date.today()
    return {
        'subscribed': True, 'id': sub.id, 'nickname': sub.nickname,
        'plan_name': sub.plan.name if sub.plan else '',
        'status': sub.status.value,
        'messages_used': sub.messages_used, 'messages_limit': sub.messages_limit,
        'days_left': (sub.expires_at - today).days if sub.expires_at else 0,
    }
