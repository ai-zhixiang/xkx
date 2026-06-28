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
import logging
from datetime import datetime, date, timedelta

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
import httpx

from app.models import get_db, Subscriber, SubOrder, Plan, OrderStatus, SubscriberStatus, PageVisit

logger = logging.getLogger(__name__)

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
        if WXPAY_ENABLED:
            raise HTTPException(400, '支付未配置')
        # 测试模式：不需要微信支付配置
        pass

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
            expires_at=today + timedelta(days=plan.months * 30),
            messages_limit=plan.monthly_messages,
            last_reset_at=today,
            xiake_points=plan.months * 3000,  # 每月 3000 虾点
        )
    else:
        sub.plan_id = plan.id
        sub.messages_limit = plan.monthly_messages
        sub.messages_used = 0
        # 续费：叠加虾点（上限不要超过配额 2 倍）
        monthly_points = plan.months * 3000
        max_points = monthly_points * 2
        sub.xiake_points = min((sub.xiake_points or 0) + monthly_points, max_points)
        old_expires = sub.expires_at if sub.expires_at and sub.expires_at > today else today
        sub.expires_at = old_expires + timedelta(days=plan.months * 30)

    db.add(sub)
    await db.flush()

    # 新用户转化追踪
    if is_new:
        try:
            from sqlalchemy import select as _sel
            vr = await db.execute(
                _sel(PageVisit).where(
                    PageVisit.openid == data.openid,
                    PageVisit.converted == False
                ).order_by(PageVisit.created_at.desc()).limit(1)
            )
            visit = vr.scalar_one_or_none()
            if visit:
                visit.converted = True
                visit.converted_at = datetime.now()
                visit.subscriber_id = sub.id
                await db.flush()
        except Exception:
            pass

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
    await db.flush()

    # 手机号绑定：下单时如有手机号，自动建立/更新通道绑定
    if hasattr(data, "phone") and data.phone:
        try:
            from app.models import AsyncSessionLocal as _asf
            from sqlalchemy import text as _t
            async with _asf() as _session:
                # Look up channel_binding by phone
                cb_row = await _session.execute(
                    _t("SELECT channel_type, channel_user_id FROM channel_bindings WHERE phone = :p AND user_account_id IS NULL LIMIT 1"),
                    {"p": data.phone},
                )
                cb = cb_row.fetchone()
                if cb:
                    await _session.execute(
                        _t("UPDATE channel_bindings SET user_account_id = :uid, openid = :oid, nickname = :nick WHERE phone = :p"),
                        {"uid": sub.id, "oid": data.openid, "nick": data.nickname or "", "p": data.phone},
                    )
                    await _session.commit()
                    logger.info(f"[支付] 手机 {data.phone[-4:]} 绑定到 subscriber {sub.id}")
        except Exception as e:
            logger.warning(f"[支付] 手机绑定失败: {e}")

    await db.commit()
    await db.refresh(order)

    # 测试模式：跳过微信支付，直接标记已付
    if not WXPAY_ENABLED:
        order.status = OrderStatus.PAID
        order.paid_at = datetime.now()
        # 激活订阅
        sub.status = SubscriberStatus.ACTIVE
        if order.new_expires_at:
            sub.expires_at = order.new_expires_at
        await db.commit()

        # 推 Bot 确认消息
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as _hc:
                from sqlalchemy import text as sa_text
                cb = await db.execute(
                    sa_text("SELECT bot_id, user_id FROM bot_accounts WHERE user_id LIKE :oid AND is_active = true LIMIT 1"),
                    {"oid": data.openid + "%"}
                )
                cb_row = cb.fetchone()
                if cb_row:
                    remain = (sub.expires_at - date.today()).days if sub.expires_at else 30
                    await _hc.post("http://127.0.0.1:9100/api/subscription-confirmed", json={
                        "bot_id": cb_row[0],
                        "to_user": cb_row[1],
                        "nickname": data.nickname or "虾友",
                        "plan_name": plan.name,
                        "remain_days": remain,
                        "expires_at": str(sub.expires_at) if sub.expires_at else "",
                    })
                    logger.info(f"[支付→Bot] 已通知 {data.openid[:12]}...")
                else:
                    logger.warning(f"[支付→Bot] 未找到 openid {data.openid[:12]} 对应的 bot")
        except Exception as _ne:
            logger.warning(f"[支付→Bot] 推确认失败: {_ne}")

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
    order.out_trade_no = out_trade_no
    await db.commit()
    body = {
        'appid': WX_APPID,
        'mchid': WX_MCHID,
        'description': f'享客虾-{plan.name}',
        'out_trade_no': out_trade_no,
        'notify_url': WX_NOTIFY_URL,
        'amount': {'total': plan.price, 'currency': 'CNY'},
        'payer': {'openid': data.openid},
    }

    body_str = json.dumps(body, ensure_ascii=False, separators=(',', ':'))

    # 签名
    nonce = _gen_nonce()
    timestamp = str(int(time.time()))
    sign_str = f'POST\n/v3/pay/transactions/jsapi\n{timestamp}\n{nonce}\n{body_str}\n'
    signature = _sign_sha256_rsa(sign_str)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                'https://api.mch.weixin.qq.com/v3/pay/transactions/jsapi',
                content=body_str.encode(),
                headers={
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'Authorization': f'WECHATPAY2-SHA256-RSA2048 mchid="{WX_MCHID}",nonce_str="{nonce}",timestamp="{timestamp}",serial_no="{WX_SERIAL_NO}",signature="{signature}"',
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
                        'timestamp': ts2,
                        'nonceStr': nonce2,
                        'package': pkg,
                        'signType': 'RSA',
                        'paySign': pay_sign,
                    },
                }
            else:
                err_detail = r.text[:500] if r.text else '无响应体'
                logger.warning(f'[支付] 微信下单失败 {r.status_code}: {err_detail}')
                return {'ok': False, 'error': f'微信下单失败: {r.status_code}', 'detail': err_detail}
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
    
    # 解密 ciphertext（AEAD_AES_256_GCM）
    ciphertext = resource.get('ciphertext', '')
    if ciphertext:
        import base64
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = resource.get('nonce', '')
        associated_data = resource.get('associated_data', '')
        key_bytes = WX_API_V3_KEY.encode('utf-8')
        aesgcm = AESGCM(key_bytes)
        # 微信回调 base64 可能不带 padding，统一补齐
        def _pad(s):
            return s + '=' * (4 - len(s) % 4) if len(s) % 4 else s
        ct_bytes = base64.b64decode(_pad(ciphertext))
        ad_bytes = base64.b64decode(_pad(associated_data)) if associated_data else None
        nonce_bytes = base64.b64decode(_pad(nonce))
        decrypted = aesgcm.decrypt(nonce_bytes, ct_bytes, ad_bytes)
        tx = json.loads(decrypted.decode('utf-8'))
    else:
        tx = resource
    
    out_trade_no = tx.get('out_trade_no', '')

    if not out_trade_no.startswith('XKX'):
        return JSONResponse({'code': 'SUCCESS'})

    # 更新订单状态
    # out_trade_no 格式: XKX{order_id}{10位时间戳}
    order_id_str = out_trade_no[3:-10] if len(out_trade_no) > 13 else ''
    try:
        order_id = int(order_id_str)
    except (ValueError, TypeError):
        return JSONResponse({'code': 'SUCCESS'})

    result = await db.execute(select(SubOrder).where(SubOrder.id == order_id))
    order = result.scalar_one_or_none()
    if order and order.status != OrderStatus.PAID:
        order.status = OrderStatus.PAID
        order.paid_at = datetime.now()
        order.transaction_id = tx.get('transaction_id', '')

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
