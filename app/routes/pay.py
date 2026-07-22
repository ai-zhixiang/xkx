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
logging.basicConfig(level=logging.INFO, format='%(name)s [%(levelname)s] %(message)s')
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
WX_NOTIFY_URL = os.getenv('WXPAY_NOTIFY_URL', 'https://ai.pangoozn.com/api/pay/notify')
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

    # 用户 - 仅查找或创建占位记录，不更新状态/点数
    result = await db.execute(
        select(Subscriber)
        .where(Subscriber.openid == data.openid)
    )
    sub = result.scalar_one_or_none()
    is_new = not sub

    today = date.today()
    # 计算续费后到期日（用于订单记录，但不更新 subscriber）
    if sub and sub.expires_at and sub.expires_at > today:
        calc_expires = sub.expires_at + timedelta(days=plan.months * 30)
    else:
        calc_expires = today + timedelta(days=plan.months * 30)

    if not sub:
        sub = Subscriber(
            openid=data.openid,
            nickname=data.nickname or f'虾客{data.openid[-4:]}',
            plan_id=plan.id,
            status=SubscriberStatus.PENDING,  # PENDING，付款后才激活
            started_at=today,
            expires_at=today,  # 占位
            messages_limit=plan.monthly_messages,
            last_reset_at=today,
            xiake_points=0,  # 付款后才加
        )
        db.add(sub)
        await db.flush()

    # 订单
    order = SubOrder(
        subscriber_id=sub.id,
        plan_id=plan.id,
        plan_name=plan.name,
        amount=plan.price,
        months=plan.months,
        status=OrderStatus.PENDING,
        payment_method='wechat',
        new_expires_at=calc_expires,
    )
    db.add(order)
    await db.flush()

    # 测试模式：跳过微信支付，直接激活
    if not WXPAY_ENABLED:
        order.status = OrderStatus.PAID
        order.paid_at = datetime.now()
        # 真正激活 subscriber
        sub.status = SubscriberStatus.ACTIVE
        sub.xiake_points = plan.months * 10000
        sub.expires_at = calc_expires
        if is_new:
            sub.started_at = today
        await db.commit()
        await db.refresh(order)

        # 推 Bot 确认消息
        try:
            async with httpx.AsyncClient(timeout=5) as _hc:
                from sqlalchemy import text as sa_text
                # 查 channel_bindings: svc_openid → channel_user_id(iLink user_id)
                _bot_row = None
                _bot_uid = ""
                _cb = await db.execute(
                    sa_text("SELECT channel_user_id FROM channel_bindings WHERE openid = :oid LIMIT 1"),
                    {"oid": data.openid}
                )
                _cb_row = _cb.fetchone()
                if _cb_row:
                    _bot_uid = _cb_row[0]
                    _ba = await db.execute(
                        sa_text("SELECT bot_id, user_id FROM bot_accounts WHERE (user_id = :uid1 OR user_id LIKE :uid2) AND is_active = true LIMIT 1"),
                        {"uid1": _bot_uid, "uid2": _bot_uid.split('@')[0] + "%"}
                    )
                    _bot_row = _ba.fetchone()
                if not _bot_row:
                    # 兜底：直接查 bot_accounts.svc_openid
                    _ba2 = await db.execute(
                        sa_text("UPDATE bot_accounts SET svc_openid = :svc WHERE svc_openid IS NULL AND user_id LIKE :oid AND is_active = true RETURNING bot_id, user_id"),
                        {"svc": data.openid, "oid": "%" + data.openid[-8:] + "%"}
                    )
                    _bot_row = _ba2.fetchone()
                if _bot_row:
                    # 回写 svc_openid 方便下次查
                    try:
                        await db.execute(
                            sa_text("UPDATE bot_accounts SET svc_openid = :svc WHERE bot_id = :bid AND (svc_openid IS NULL OR svc_openid = '')"),
                            {"svc": data.openid, "bid": _bot_row[0]}
                        )
                        await db.commit()
                    except:
                        pass
                    remain = (sub.expires_at - date.today()).days if sub.expires_at else 30
                    real_nick = data.nickname or ""
                    try:
                        _cn = await db.execute(
                            sa_text("SELECT nickname FROM channel_bindings WHERE openid = :oid LIMIT 1"),
                            {"oid": data.openid}
                        )
                        _cnr = _cn.fetchone()
                        if _cnr and _cnr[0]:
                            real_nick = _cnr[0]
                    except:
                        pass
                    if not real_nick:
                        real_nick = sub.nickname or "虾友"
                    pts = sub.xiake_points or 0
                    await _hc.post("http://127.0.0.1:9100/api/subscription-confirmed", json={
                        "bot_id": _bot_row[0],
                        "to_user": _bot_row[1],
                        "nickname": real_nick,
                        "plan_name": plan.name,
                        "remain_days": remain,
                        "expires_at": str(sub.expires_at) if sub.expires_at else "",
                        "xiake_points": pts,
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
        'description': f'享客虾-{plan.name.replace(chr(0x1f99e),chr(0x200d) if False else chr(0x200b))}',
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
    # 注意：nonce 直接 utf-8 字节，不 base64 解码
    ciphertext = resource.get('ciphertext', '')
    tx = None
    if ciphertext:
        try:
            import base64
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce = resource.get('nonce', '')
            associated_data = resource.get('associated_data', '')
            key_bytes = WX_API_V3_KEY.encode('utf-8')
            aesgcm = AESGCM(key_bytes)
            ct_bytes = base64.b64decode(ciphertext)
            nonce_bytes = nonce.encode('utf-8')
            ad_bytes = associated_data.encode('utf-8') if associated_data else None
            decrypted = aesgcm.decrypt(nonce_bytes, ct_bytes, ad_bytes)
            tx = json.loads(decrypted.decode('utf-8'))
        except Exception as e:
            import traceback
            print(f'[支付回调] 解密失败: type={type(e).__name__} msg={e}', flush=True)
            traceback.print_exc()
            return JSONResponse({'code': 'SUCCESS'})
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
            # 加虾点
            monthly_points = order.months * 3000
            max_points = monthly_points * 2
            sub.xiake_points = min((sub.xiake_points or 0) + monthly_points, max_points)

        await db.commit()

        # 推广返佣结算
        try:
            async with httpx.AsyncClient(timeout=5) as _ref:
                await _ref.post(
                    "http://127.0.0.1:8001/api/referral/settle",
                    params={"order_id": order_id}
                )
        except Exception:
            pass  # 返佣不影响主流程

        # 推 Bot 确认消息
        if sub and order:
            try:
                today = date.today()
                remain = (sub.expires_at - today).days if sub.expires_at and sub.expires_at > today else 30
                async with httpx.AsyncClient(timeout=5) as _hc:
                    from sqlalchemy import text as sa_text
                    # 查 channel_bindings: svc_openid → channel_user_id(iLink user_id)
                    _bot_row = None
                    _cb = await db.execute(
                        sa_text("SELECT channel_user_id FROM channel_bindings WHERE openid = :oid LIMIT 1"),
                        {"oid": sub.openid}
                    )
                    _cb_row = _cb.fetchone()
                    if _cb_row:
                        _bot_uid = _cb_row[0]
                        _ba = await db.execute(
                            sa_text("SELECT bot_id, user_id FROM bot_accounts WHERE (user_id = :uid1 OR user_id LIKE :uid2) AND is_active = true LIMIT 1"),
                            {"uid1": _bot_uid, "uid2": _bot_uid.split('@')[0] + "%"}
                        )
                        _bot_row = _ba.fetchone()
                    if not _bot_row:
                        # 兜底：直接查 bot_accounts.svc_openid
                        _ba2 = await db.execute(
                            sa_text("UPDATE bot_accounts SET svc_openid = :svc WHERE svc_openid IS NULL AND user_id LIKE :oid AND is_active = true RETURNING bot_id, user_id"),
                            {"svc": sub.openid, "oid": "%" + sub.openid[-8:] + "%"}
                        )
                        _bot_row = _ba2.fetchone()
                    if _bot_row:
                        real_nick = sub.nickname or ""
                        try:
                            cbn = await db.execute(
                                sa_text("SELECT nickname FROM channel_bindings WHERE openid = :oid LIMIT 1"),
                                {"oid": sub.openid}
                            )
                            cbn_row = cbn.fetchone()
                            if cbn_row and cbn_row[0]:
                                real_nick = cbn_row[0]
                        except:
                            pass
                        if not real_nick:
                            real_nick = "虾友"
                        plan_name = order.plan_name or "会员"
                        pts = sub.xiake_points or 0
                        await _hc.post("http://127.0.0.1:9100/api/subscription-confirmed", json={
                            "bot_id": _bot_row[0],
                            "to_user": _bot_row[1],
                            "nickname": real_nick,
                            "plan_name": plan_name,
                            "remain_days": remain,
                            "expires_at": str(sub.expires_at) if sub.expires_at else "",
                            "xiake_points": pts,
                        })
                        logger.info(f"[支付回调→Bot] 已通知 {sub.openid[:12] if sub.openid else ''}...")
                    else:
                        logger.warning(f"[支付回调→Bot] 未找到 openid 对应的 bot")
            except Exception as _ne:
                logger.warning(f"[支付回调→Bot] 推确认失败: {_ne}")

    return JSONResponse({'code': 'SUCCESS'})


# ===== 微信支付订单查询 =====

@router.post('/api/pay/query')
async def query_order(data: dict):
    """查询微信支付订单状态"""
    out_trade_no = data.get('out_trade_no', '')
    if not out_trade_no:
        return {'ok': False, 'error': '缺少订单号'}
    
    private_key = _load_private_key()
    if not private_key:
        return {'ok': False, 'error': '商户证书未配置'}
    
    nonce = _gen_nonce()
    timestamp = str(int(time.time()))
    path = f'/v3/pay/transactions/out-trade-no/{out_trade_no}'
    sign_str = f'GET\\n{path}\\n{timestamp}\\n{nonce}\\n\\n'
    signature = _sign_sha256_rsa(sign_str)
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f'https://api.mch.weixin.qq.com{path}?mchid={WX_MCHID}',
                headers={
                    'Authorization': f'WECHATPAY2-SHA256-RSA2048 mchid="{WX_MCHID}",nonce_str="{nonce}",timestamp="{timestamp}",serial_no="{WX_SERIAL_NO}",signature="{signature}"',
                },
            )
            return {'ok': r.status_code == 200, 'status': r.status_code, 'data': r.json() if r.status_code == 200 else r.text[:500]}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


# ===== 微信支付退款 =====

@router.post('/api/pay/refund')
async def refund_order(data: dict):
    """退款"""
    order_id = data.get('order_id', 0)
    if not order_id:
        return {'ok': False, 'error': '缺少订单ID'}
    
    db = await anext(get_db())
    try:
        result = await db.execute(select(SubOrder).where(SubOrder.id == order_id))
        order = result.scalar_one_or_none()
        if not order:
            return {'ok': False, 'error': '订单不存在'}
        if order.status != OrderStatus.PAID:
            return {'ok': False, 'error': '订单未支付'}
        
        out_trade_no = order.out_trade_no or f'XKX{order.id}'
        refund_no = f'REF{order.id}{int(time.time())}'
        total_fee = order.amount
        refund_fee = total_fee
        
        private_key = _load_private_key()
        if not private_key:
            return {'ok': False, 'error': '商户证书未配置'}
        
        nonce = _gen_nonce()
        timestamp = str(int(time.time()))
        body = {
            'out_trade_no': out_trade_no,
            'out_refund_no': refund_no,
            'amount': {'refund': refund_fee, 'total': total_fee, 'currency': 'CNY'},
        }
        body_str = json.dumps(body, ensure_ascii=False, separators=(',', ':'))
        sign_str = f'POST\\n/v3/refund/domestic/refunds\\n{timestamp}\\n{nonce}\\n{body_str}\\n'
        signature = _sign_sha256_rsa(sign_str)
        
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                'https://api.mch.weixin.qq.com/v3/refund/domestic/refunds',
                content=body_str.encode(),
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'WECHATPAY2-SHA256-RSA2048 mchid="{WX_MCHID}",nonce_str="{nonce}",timestamp="{timestamp}",serial_no="{WX_SERIAL_NO}",signature="{signature}"',
                },
            )
            if r.status_code == 200:
                refund_data = r.json()
                order.status = OrderStatus.REFUNDED
                order.refund_status = 'SUCCESS'
                order.refund_id = refund_data.get('refund_id', '')
                await db.commit()
                
                # 扣减 subscriber 点数
                result2 = await db.execute(select(Subscriber).where(Subscriber.id == order.subscriber_id))
                sub = result2.scalar_one_or_none()
                if sub:
                    sub.xiake_points = max(0, (sub.xiake_points or 0) - 3000)
                    await db.commit()
                
                return {'ok': True, 'refund_id': refund_data.get('refund_id', ''), 'status': refund_data.get('status', '')}
            else:
                return {'ok': False, 'error': f'退款失败 ({r.status_code})', 'detail': r.text[:500]}
    except Exception as e:
        return {'ok': False, 'error': str(e)}
    finally:
        await db.close()


# ===== 健康检查 =====

@router.get('/api/pay/config')
async def pay_config():
    return {
        'appid': WX_APPID[:6] + '***' if WX_APPID else '未配置',
        'mchid': WX_MCHID,
        'has_key': bool(_load_private_key()),
        'notify_url': WX_NOTIFY_URL,
    }
