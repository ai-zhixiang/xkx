"""
享客虾 — 管理后台API
订阅用户管理 / 套餐管理 / 统计
"""
import os
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.models import (
    get_db, Plan, Subscriber, SubOrder, ChatConversation,
    SubscriberStatus, OrderStatus, SEED_PLANS, Notification
)

router = APIRouter()

ADMIN_USER = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASS = os.getenv('ADMIN_PASSWORD', 'admin888')


def check_auth(request: Request):
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        raise HTTPException(401)
    import base64
    expected = base64.b64encode(f'{ADMIN_USER}:{ADMIN_PASS}'.encode()).decode()
    if auth[7:] != expected:
        raise HTTPException(401)


# ===== 套餐管理 =====

@router.get('/plans')
async def list_plans(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Plan).order_by(Plan.sort_order))
    return [{'id': p.id, 'name': p.name, 'price': p.price,
             'price_yuan': f'{p.price/100:.1f}'.rstrip('0').rstrip('.'),
             'monthly_messages': p.monthly_messages, 'sort_order': p.sort_order,
             'is_active': p.is_active} for p in result.scalars().all()]


@router.post('/plans')
async def create_plan(data: dict, request: Request, db: AsyncSession = Depends(get_db)):
    check_auth(request)
    plan = Plan(**{k: data[k] for k in ['name','price','monthly_messages','sort_order'] if k in data})
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return {'ok': True, 'id': plan.id}


@router.post('/plans/{plan_id}/toggle')
async def toggle_plan(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    check_auth(request)
    plan = await db.get(Plan, plan_id)
    if plan:
        plan.is_active = not plan.is_active
        await db.commit()
    return {'ok': True}


# ===== 订阅用户管理 =====

def _sub_dict(s: Subscriber):
    today = date.today()
    return {
        'id': s.id, 'openid': s.openid[-8:], 'nickname': s.nickname,
        'plan_name': s.plan.name if s.plan else '', 'plan_id': s.plan_id,
        'status': s.status.value if s.status else '',
        'messages_used': s.messages_used, 'messages_limit': s.messages_limit,
        'usage_pct': round(s.messages_used/max(s.messages_limit,1)*100),
        'total_messages': s.total_messages,
        'started_at': str(s.started_at) if s.started_at else '',
        'expires_at': str(s.expires_at) if s.expires_at else '',
        'days_left': (s.expires_at - today).days if s.expires_at else 0,
        'created_at': str(s.created_at)[:19] if s.created_at else '',
    }


@router.get('/subscribers')
async def list_subscribers(
    status: Optional[str] = None, q: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Subscriber).options(selectinload(Subscriber.plan)).order_by(Subscriber.created_at.desc())
    if status:
        stmt = stmt.where(Subscriber.status == status)
    result = await db.execute(stmt)
    subs = result.scalars().all()
    data = [_sub_dict(s) for s in subs]
    if q:
        ql = q.lower()
        data = [s for s in data if ql in s['nickname'].lower() or ql in s['openid'].lower()]
    return data


@router.get('/subscribers/{sub_id}')
async def get_subscriber(sub_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Subscriber).options(selectinload(Subscriber.plan)).where(Subscriber.id == sub_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(404)
    return _sub_dict(sub)


@router.post('/subscribers/{sub_id}/expire')
async def expire_subscriber(sub_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    check_auth(request)
    sub = await db.get(Subscriber, sub_id)
    if sub:
        sub.status = SubscriberStatus.EXPIRED
        await db.commit()
    return {'ok': True}


@router.post('/subscribers/{sub_id}/activate')
async def activate_subscriber(sub_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    check_auth(request)
    sub = await db.get(Subscriber, sub_id)
    if sub:
        sub.status = SubscriberStatus.ACTIVE
        await db.commit()
    return {'ok': True}


@router.post('/subscribers/{sub_id}/renew')
async def renew_subscriber(sub_id: int, data: dict, request: Request, db: AsyncSession = Depends(get_db)):
    """手动续费"""
    check_auth(request)
    months = data.get('months', 1)
    sub = await db.get(Subscriber, sub_id)
    if not sub:
        raise HTTPException(404)
    plan = await db.get(Plan, sub.plan_id)
    if not plan:
        raise HTTPException(400)
    old_expires = sub.expires_at if sub.expires_at and sub.expires_at > date.today() else date.today()
    sub.expires_at = old_expires + timedelta(days=30 * months)
    sub.status = SubscriberStatus.ACTIVE
    sub.messages_used = 0
    order = SubOrder(
        subscriber_id=sub.id, plan_id=plan.id, plan_name=plan.name,
        amount=plan.price * months, months=months, status=OrderStatus.PAID,
        new_expires_at=sub.expires_at, paid_at=datetime.utcnow(),
    )
    db.add(order)
    await db.commit()
    return {'ok': True}


# ===== 统计 =====

@router.get('/stats')
async def get_stats(db: AsyncSession = Depends(get_db)):
    r1 = await db.execute(select(func.count(Subscriber.id)))
    total = r1.scalar()
    r2 = await db.execute(select(func.count(Subscriber.id)).where(Subscriber.status == SubscriberStatus.ACTIVE))
    active = r2.scalar()
    r4 = await db.execute(select(func.count(Subscriber.id)).where(Subscriber.status == SubscriberStatus.EXPIRED))
    expired = r4.scalar()
    r5 = await db.execute(select(func.coalesce(func.sum(Subscriber.total_messages), 0)))
    total_msgs = r5.scalar()

    today = date.today()
    r6 = await db.execute(
        select(func.count(Subscriber.id)).where(
            Subscriber.status == SubscriberStatus.ACTIVE,
            Subscriber.expires_at <= today + timedelta(days=7),
            Subscriber.expires_at > today,
        )
    )
    expiring = r6.scalar()

    return {
        'total_subscribers': total, 'active': active,
        'expired': expired, 'expiring_soon': expiring, 'total_messages': total_msgs,
    }


# ===== 订单 =====

@router.get('/orders')
async def list_orders(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(SubOrder).order_by(SubOrder.created_at.desc()).limit(100))
    orders = result.scalars().all()
    return [{
        'id': o.id, 'subscriber_id': o.subscriber_id,
        'plan_name': o.plan_name, 'amount': o.amount,
        'amount_yuan': f'{o.amount/100:.2f}', 'months': o.months,
        'status': o.status.value, 'payment_method': o.payment_method,
        'created_at': str(o.created_at)[:19] if o.created_at else '',
        'paid_at': str(o.paid_at)[:19] if o.paid_at else '',
    } for o in orders]
