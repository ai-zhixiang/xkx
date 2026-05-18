"""
享客虾 — 微信支付
v0.1：JSAPI 支付 · 复用智享家商户号
"""
import os
import json
import time
import hashlib
import string
import secrets
from datetime import datetime, date, timedelta

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
import httpx

from app.models import get_db, Subscriber, SubOrder, Plan, OrderStatus, SubscriberStatus

router = APIRouter()

WX_APPID = os.getenv('WECHAT_APPID', '')
WX_MCHID = os.getenv('WXPAY_MCHID', '')
WX_API_V3_KEY = os.getenv('WXPAY_API_V3_KEY', '')
WX_SERIAL_NO = os.getenv('WXPAY_MCH_SERIAL_NO', '')
WX_PRIVATE_KEY_PATH = os.getenv('WXPAY_PRIVATE_KEY_PATH', '/etc/wechat/apiclient_key.pem')
WX_NOTIFY_URL = os.getenv('WXPAY_NOTIFY_URL', 'https://hai.pangoozn.com/xkx/api/pay/notify')
WXPAY_ENABLED = os.getenv('WXPAY_ENABLED', 'true').lower() in ('true', '1', 'yes')


def _load_private_key():
    try:
        with open(WX_PRIVATE_KEY_PATH) as f:
            return f.read()
    except:
        return None


def _gen_nonce(length=32):
    return ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(length))


def _sign_sha256_rsa(message: str) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.backends import default_backend
    import base64

    key_data = _load_private_key()
    if not key_data:
        return ''
    private_key = serialization.load_pem_private_key(key_data.encode(), password=None, backend=default_backend())
    signature = private_key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode()


class CreateOrderRequest(BaseModel):
    plan_id: int
    openid: str
    nickname: str = ''


@router.post('/api/pay/create')
async def create_order(data: CreateOrderRequest, db: AsyncSession = Depends(get_db)):
    """创建预付单，返回 JSAPI 参数"""
    if not WX_APPID or not WX_MCHID:
        raise HTTPException(400, '支付未配置')

    # 套餐
    result = await db.execute(select(Plan).where(Plan.id == data.plan_id, Plan.is_active == True))
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(400, '套餐不存在')
    if plan.price <= 0:
        raise HTTPException(400, '该套餐无需支付')

    # 用户
    result = await db.execute(
        select(Subscriber).options(selectinload(Subscriber.plan))
        .where(Subscriber.openid == data.openid)
    )
    sub = result.scalar_one_or_none()
    is_new = not sub

    today = date.today()

    if not sub:
        sub = Subscriber(
            openid=data.openid,
            nickname=data.nickname or f'虾客{data.openid[-4:]}',
            plan_id=plan.id,
            status=SubscriberStatus.ACTIVE,
            started_at=today,
            expires_at=today + timedelta(days=30),
            messages_limit=plan.monthly_messages,
            last_reset_at=today,
        )
    else:
        sub.plan_id = plan.id
        sub.messages_limit = plan.monthly_messages
        sub.messages_used = 0
        old_expires = sub.expires_at if sub.expires_at and sub.expires_at > today else today
        sub.expires_at = old_expires + timedelta(days=30)

    db.add(sub)
    await db.flush()

    # 订单
    order = SubOrder(
        subscriber_id=sub.id,
        plan_id=plan.id,
        plan_name=plan.name,
        amount=plan.price,
        months=1,
        status=OrderStatus.PENDING,
        payment_method='wechat',
        new_expires_at=sub.expires_at,
    )
    db.add(order)
    await db.commit()
    await db.refresh(order)

    # 测试模式：跳过微信支付，直接标记已付
    if not WXPAY_ENABLED:
        order.status = OrderStatus.PAID
        order.paid_at = datetime.utcnow()
        # 激活订阅
        sub.status = SubscriberStatus.ACTIVE
        if order.new_expires_at:
            sub.expires_at = order.new_expires_at
        await db.commit()
        return {
            'ok': True,
            'test_mode': True,
            'order_id': order.id,
            'subscriber_id': sub.id,
            'is_new': is_new,
            'message': '测试模式：已跳过微信支付，订阅已激活',
            'jsapi': None,
        }

    # 构建微信 JSAPI 下单
    out_trade_no = f'XKX{order.id}{int(time.time())}'
    amount_yuan = plan.price / 100

    body = {
        'appid': WX_APPID,
        'mchid': WX_MCHID,
        'description': f'享客虾-{plan.name}',
        'out_trade_no': out_trade_no,
        'notify_url': WX_NOTIFY_URL,
        'amount': {'total': plan.price, 'currency': 'CNY'},
        'payer': {'openid': data.openid},
    }

    # 签名
    nonce = _gen_nonce()
    timestamp = str(int(time.time()))
    sign_str = f'POST\n/v3/pay/transactions/jsapi\n{timestamp}\n{nonce}\n{json.dumps(body)}\n'
    signature = _sign_sha256_rsa(sign_str)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                'https://api.mch.weixin.qq.com/v3/pay/transactions/jsapi',
                json=body,
                headers={
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'Authorization': f'WECHATPAY2-SHA256-RSA2048 mchid="{WX_MCHID}",nonce_str="{nonce}",signature="{signature}",timestamp="{timestamp}",serial_no="{WX_SERIAL_NO}"',
                },
            )
            if r.status_code == 200:
                prepay = r.json()

                # 前端需要：appId, timeStamp, nonceStr, package, signType, paySign
                pkg = f'prepay_id={prepay["prepay_id"]}'
                ts2 = str(int(time.time()))
                nonce2 = _gen_nonce()
                pay_sign_str = f'{WX_APPID}\n{ts2}\n{nonce2}\n{pkg}\n'
                pay_sign = _sign_sha256_rsa(pay_sign_str)

                return {
                    'ok': True,
                    'order_id': order.id,
                    'subscriber_id': sub.id,
                    'is_new': is_new,
                    'jsapi': {
                        'appId': WX_APPID,
                        'timeStamp': ts2,
                        'nonceStr': nonce2,
                        'package': pkg,
                        'signType': 'RSA',
                        'paySign': pay_sign,
                    },
                }
            else:
                return {'ok': False, 'error': f'微信下单失败: {r.status_code}', 'detail': r.text[:200]}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


@router.post('/api/pay/notify')
async def pay_notify(request: Request, db: AsyncSession = Depends(get_db)):
    """微信支付回调通知"""
    body = await request.body()
    data = json.loads(body)

    if data.get('event_type') != 'TRANSACTION.SUCCESS':
        return JSONResponse({'code': 'SUCCESS'})

    resource = data.get('resource', {})
    # 简化处理：直接读明文（生产环境需解密）
    tx = resource if 'out_trade_no' in resource else json.loads(resource.get('ciphertext', '{}'))
    out_trade_no = tx.get('out_trade_no', '')

    if not out_trade_no.startswith('XKX'):
        return JSONResponse({'code': 'SUCCESS'})

    # 更新订单状态
    order_id_str = out_trade_no[3:].split(str(int(time.time()))[:5])[0] if out_trade_no[3:] else ''
    try:
        order_id = int(order_id_str)
    except ValueError:
        return JSONResponse({'code': 'SUCCESS'})

    result = await db.execute(select(SubOrder).where(SubOrder.id == order_id))
    order = result.scalar_one_or_none()
    if order and order.status != OrderStatus.PAID:
        order.status = OrderStatus.PAID
        order.paid_at = datetime.utcnow()

        # 激活订阅
        result = await db.execute(
            select(Subscriber).where(Subscriber.id == order.subscriber_id)
        )
        sub = result.scalar_one_or_none()
        if sub:
            sub.status = SubscriberStatus.ACTIVE
            if order.new_expires_at:
                sub.expires_at = order.new_expires_at

        await db.commit()

    return JSONResponse({'code': 'SUCCESS'})


# ===== 健康检查 =====

@router.get('/api/pay/config')
async def pay_config():
    return {
        'appid': WX_APPID[:6] + '***' if WX_APPID else '未配置',
        'mchid': WX_MCHID,
        'has_key': bool(_load_private_key()),
        'notify_url': WX_NOTIFY_URL,
    }
