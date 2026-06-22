"""
享客虾 — 管理后台API
订阅用户管理 / 套餐管理 / 统计
"""
import os
import time
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


@router.post('/subscribers/{sub_id}/sync-wechat')
async def sync_wechat_info(sub_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """同步微信昵称"""
    check_auth(request)
    sub = await db.get(Subscriber, sub_id)
    if not sub:
        raise HTTPException(404)
    import httpx
    async with httpx.AsyncClient(timeout=10) as c:
        tr = await c.get('https://hai.pangoozn.com/api/wechat/access-token/status')
        token = tr.json().get('access_token', '')
        if token:
            ir = await c.get('https://api.weixin.qq.com/cgi-bin/user/info',
                params={'access_token': token, 'openid': sub.openid, 'lang': 'zh_CN'})
            info = ir.json()
            if info.get('subscribe') and info.get('nickname'):
                sub.nickname = info['nickname']
    await db.commit()
    return {'ok': True, 'nickname': sub.nickname}


@router.post('/subscribers/{sub_id}/renew')
async def renew_subscriber(sub_id: int, data: dict, request: Request, db: AsyncSession = Depends(get_db)):
    """手动续费/加天数/加条数"""
    check_auth(request)
    months = data.get('months', 0)
    days = data.get('days', 0)
    add_messages = data.get('add_messages', 0)

    sub = await db.get(Subscriber, sub_id)
    if not sub:
        raise HTTPException(404)

    if days:
        old_expires = sub.expires_at if sub.expires_at and sub.expires_at > date.today() else date.today()
        sub.expires_at = old_expires + timedelta(days=days)
        sub.status = SubscriberStatus.ACTIVE

    if months:
        old_expires = sub.expires_at if sub.expires_at and sub.expires_at > date.today() else date.today()
        sub.expires_at = old_expires + timedelta(days=30 * months)
        sub.status = SubscriberStatus.ACTIVE
        sub.messages_used = 0
        plan = await db.get(Plan, sub.plan_id)
        if plan:
            order = SubOrder(
                subscriber_id=sub.id, plan_id=plan.id, plan_name=plan.name,
                amount=plan.price * months, months=months, status=OrderStatus.PAID,
                new_expires_at=sub.expires_at, paid_at=datetime.now(),
            )
            db.add(order)

    if add_messages:
        sub.messages_limit = (sub.messages_limit or 0) + add_messages

    await db.commit()
    return {'ok': True}


# ===== 统计 =====

@router.get('/documents')
async def admin_list_documents(
    page: int = 1, page_size: int = 50,
    db: AsyncSession = Depends(get_db)
):
    """管理后台文档列表"""
    from app.models import Document
    total_q = select(func.count(Document.id)).where(Document.is_deleted == False)
    total = (await db.execute(total_q)).scalar()
    
    q = select(Document).where(Document.is_deleted == False)\
        .order_by(Document.created_at.desc())\
        .offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    docs = result.scalars().all()
    
    return {
        'docs': [{
            'id': d.id, 'subscriber_id': d.subscriber_id,
            'title': d.title, 'category': d.category,
            'file_size': d.file_size,
            'created_at': str(d.created_at)[:19] if d.created_at else '',
        } for d in docs],
        'total': total
    }


@router.delete('/documents/{doc_id}')
async def admin_delete_document(doc_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """管理后台删除文档"""
    check_auth(request)
    from app.models import Document
    doc = await db.get(Document, doc_id)
    if doc:
        doc.is_deleted = True
        await db.commit()
    return {'ok': True}


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
    result = await db.execute(
        select(SubOrder).options(selectinload(SubOrder.subscriber))
        .order_by(SubOrder.created_at.desc()).limit(100)
    )
    orders = result.scalars().all()
    return [{
        'id': o.id, 'subscriber_id': o.subscriber_id,
        'openid': o.subscriber.openid[-8:] if o.subscriber else '',
        'nickname': o.subscriber.nickname if o.subscriber else '',
        'plan_name': o.plan_name, 'amount': o.amount,
        'amount_yuan': f'{o.amount/100:.2f}', 'months': o.months,
        'status': o.status.value, 'payment_method': o.payment_method,
        'out_trade_no': o.out_trade_no or '',
        'transaction_id': o.transaction_id or '',
        'refund_status': o.refund_status or '',
        'refund_amount': o.refund_amount,
        'refund_amount_yuan': f'{o.refund_amount/100:.2f}' if o.refund_amount else '',
        'created_at': str(o.created_at)[:19] if o.created_at else '',
        'paid_at': str(o.paid_at)[:19] if o.paid_at else '',
    } for o in orders]


# ===== 微信支付退款 =====

class RefundRequest(BaseModel):
    amount: int = 0  # 退款金额(分)，0=全额退

@router.post('/orders/{order_id}/refund')
async def refund_order(order_id: int, data: RefundRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """微信支付退款"""
    check_auth(request)
    
    result = await db.execute(
        select(SubOrder).options(selectinload(SubOrder.subscriber))
        .where(SubOrder.id == order_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(404, '订单不存在')
    if order.status != OrderStatus.PAID:
        raise HTTPException(400, '仅已支付订单可退款')
    if order.refund_status == 'success':
        raise HTTPException(400, '已退款')
    
    refund_amount = data.amount if data.amount > 0 else order.amount
    
    # 无微信订单号 → 软退款（测试模式订单，直接标记）
    if not order.out_trade_no:
        order.refund_status = 'success'
        order.refund_id = f'RF_SOFT_{order.id}_{int(time.time())}'
        order.refund_amount = refund_amount
        
        # 到期日回退（按比例）
        if order.subscriber and order.months > 0:
            ratio = refund_amount / order.amount
            days_back = int(30 * order.months * ratio)
            order.subscriber.expires_at = max(
                order.subscriber.expires_at - timedelta(days=days_back),
                date.today()
            )
        
        await db.commit()
        return {
            'ok': True,
            'soft_refund': True,
            'refund_id': order.refund_id,
            'amount': f'{refund_amount/100:.2f}',
            'order_id': order.id,
            'message': '测试模式订单 · 已标记退款 · 到期日已回退',
        }
    
    # 有微信订单号 → 调微信退款 API
    from app.routes.pay import _gen_nonce, _sign_sha256_rsa
    WX_MCHID = os.getenv('WXPAY_MCHID', '')
    WX_SERIAL_NO = os.getenv('WXPAY_MCH_SERIAL_NO', '')

    out_refund_no = f'RF{order.id}{int(time.time())}'

    import httpx, json
    body = {
        'out_trade_no': order.out_trade_no,
        'out_refund_no': out_refund_no,
        'amount': {
            'total': order.amount,
            'refund': refund_amount,
            'currency': 'CNY',
        },
    }
    body_str = json.dumps(body, ensure_ascii=False, separators=(',', ':'))
    
    nonce = _gen_nonce()
    timestamp = str(int(time.time()))
    sign_str = f'POST\n/v3/refund/domestic/refunds\n{timestamp}\n{nonce}\n{body_str}\n'
    signature = _sign_sha256_rsa(sign_str)
    
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                'https://api.mch.weixin.qq.com/v3/refund/domestic/refunds',
                content=body_str.encode(),
                headers={
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'Authorization': f'WECHATPAY2-SHA256-RSA2048 mchid="{WX_MCHID}",nonce_str="{nonce}",timestamp="{timestamp}",serial_no="{WX_SERIAL_NO}",signature="{signature}"',
                },
            )
            if r.status_code == 200:
                refund_data = r.json()
                order.refund_status = 'success'
                order.refund_id = refund_data.get('refund_id', out_refund_no)
                order.refund_amount = refund_amount
                
                # 到期日回退（按比例）
                if order.subscriber and order.months > 0:
                    ratio = refund_amount / order.amount
                    days_back = int(30 * order.months * ratio)
                    order.subscriber.expires_at = max(
                        order.subscriber.expires_at - timedelta(days=days_back),
                        date.today()
                    )
                
                await db.commit()
                return {
                    'ok': True,
                    'refund_id': order.refund_id,
                    'amount': f'{refund_amount/100:.2f}',
                    'order_id': order.id,
                }
            else:
                order.refund_status = 'failed'
                await db.commit()
                return {'ok': False, 'error': f'微信退款失败: {r.status_code}', 'detail': r.text[:300]}
    except Exception as e:
        order.refund_status = 'failed'
        await db.commit()
        return {'ok': False, 'error': str(e)}
