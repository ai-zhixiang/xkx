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
        'original_price': None,  # 合规：不展示虚假原价锚定
        'months': p.months,
        'sort_order': p.sort_order,
        'price_yuan': f'{p.price/100:.1f}'.rstrip('0').rstrip('.'),
        'original_yuan': None,  # 合规：不展示虚假原价锚定
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
        'avatar_url': sub.avatar_url or '',
        'plan_name': sub.plan.name if sub.plan else '',
        'status': sub.status.value,
        'messages_used': sub.messages_used, 'messages_limit': sub.messages_limit,
        'days_left': (sub.expires_at - today).days if sub.expires_at else 0,
    }


@router.post('/api/me/sync')
async def sync_my_info(openid: str = '', db: AsyncSession = Depends(get_db)):
    """从微信同步头像昵称"""
    if not openid:
        raise HTTPException(400, 'missing openid')
    result = await db.execute(
        select(Subscriber).where(Subscriber.openid == openid)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        return {'ok': False, 'error': '未找到订阅记录'}
    
    import httpx, os
    appid = os.getenv('WECHAT_APPID', '')
    secret = os.getenv('WECHAT_APPSECRET', '')
    
    # 自己拿 access_token（不依赖 ailuckycards cache）
    async with httpx.AsyncClient(timeout=10) as c:
        tr = await c.post('https://api.weixin.qq.com/cgi-bin/stable_token',
            json={'grant_type': 'client_credential', 'appid': appid, 'secret': secret,
                  'force_refresh': True})
        token_data = tr.json()
        token = token_data.get('access_token', '')
        if not token:
            return {'ok': False, 'error': f"获取token失败: {token_data.get('errmsg','')}"}
        
        ir = await c.get('https://api.weixin.qq.com/cgi-bin/user/info',
            params={'access_token': token, 'openid': sub.openid, 'lang': 'zh_CN'})
        info = ir.json()
        if info.get('subscribe') and info.get('nickname'):
            sub.nickname = info['nickname']
            if info.get('headimgurl'):
                sub.avatar_url = info['headimgurl']
            await db.commit()
            return {
                'ok': True,
                'nickname': sub.nickname,
                'avatar_url': sub.avatar_url or ''
            }
        return {'ok': False, 'error': '微信接口未返回昵称', 'raw': str(info)[:200]}


@router.get('/api/me/wechat-follow')
async def check_wechat_follow(openid: str = '', db: AsyncSession = Depends(get_db)):
    """检查用户是否已关注智享家公众号"""
    if not openid:
        return {'followed': False, 'error': 'missing openid'}
    result = await db.execute(
        select(Subscriber).where(Subscriber.openid == openid)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        return {'followed': False, 'error': 'no subscriber'}
    
    import httpx, os
    appid = os.getenv('WECHAT_APPID', '')
    secret = os.getenv('WECHAT_APPSECRET', '')
    if not appid or not secret:
        return {'followed': True, 'error': 'no wechat config', 'fallback': True}
    
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            tr = await c.post('https://api.weixin.qq.com/cgi-bin/stable_token',
                json={'grant_type': 'client_credential', 'appid': appid, 'secret': secret})
            token = tr.json().get('access_token', '')
            if not token:
                return {'followed': True, 'error': 'token fail', 'fallback': True}
            ir = await c.get('https://api.weixin.qq.com/cgi-bin/user/info',
                params={'access_token': token, 'openid': sub.openid, 'lang': 'zh_CN'})
            info = ir.json()
            return {'followed': bool(info.get('subscribe', 0))}
    except Exception:
        return {'followed': True, 'fallback': True, 'error': 'api error'}
