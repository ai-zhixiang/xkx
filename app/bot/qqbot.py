"""
享客虾 — QQ Bot 通道
通过 QQ 官方开放平台 WebSocket 接收消息，复用共享 AI 服务。
"""
import os
import json
import uuid
import asyncio
import traceback
from datetime import datetime, date

import httpx
import websockets

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import (
    AsyncSessionLocal, Subscriber, ChatConversation,
    SubscriberStatus, QQBindCode
)
from app.services.ai import ai_chat, PRODUCT_URL

# QQ Bot 配置
QQ_APP_ID = os.getenv('QQ_BOT_APPID', '')
QQ_APP_SECRET = os.getenv('QQ_BOT_APPSECRET', '')
QQ_BOT_ENABLED = bool(QQ_APP_ID and QQ_APP_SECRET)

# QQ Bot API 地址
QQ_TOKEN_URL = 'https://bots.qq.com/app/getAppAccessToken'
QQ_GATEWAY_URL = 'https://api.sgroup.qq.com/gateway'
QQ_SEND_MSG_URL = 'https://api.sgroup.qq.com/v2/users/{openid}/messages'

# QQ 通道专用文案
QQ_NOT_SUBSCRIBED = (
    '🦞 嗨！我是享客虾，你的私人AI秘书。\n\n'
    '开通后即可开始对话：\n'
    '基础版 · ¥9.9/月 · 500条\n'
    '标准版 · ¥19.9/月 · 2000条\n\n'
    f'👉 请在微信中打开：{PRODUCT_URL}'
)

QQ_NEED_BIND = (
    '🦞 你还没绑定微信账号！\n\n'
    '请在微信「智享家」公众号中发送：\n'
    '绑定 {code}\n\n'
    '(如果你还没订阅，请先在微信中开通享客虾)'
)

QQ_BIND_OK = (
    '✅ 绑定成功！现在可以开始对话了。'
)

QQ_QUOTA_EXHAUSTED = (
    '🦞 本月 {limit} 条额度已用完。\n\n'
    f'👉 续费或升级套餐：{PRODUCT_URL}'
)


async def _get_access_token() -> str:
    """获取 QQ Bot access_token"""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(QQ_TOKEN_URL, json={
            'appId': QQ_APP_ID,
            'clientSecret': QQ_APP_SECRET,
        })
        r.raise_for_status()
        data = r.json()
        return data['access_token']


async def _get_ws_url(token: str) -> str:
    """获取 WebSocket 网关地址"""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(QQ_GATEWAY_URL,
            headers={'Authorization': f'QQBot {token}'})
        r.raise_for_status()
        return r.json()['url']


async def _send_qq_message(openid: str, content: str, token: str, msg_id: str = '') -> bool:
    """通过 QQ Bot API 发送私聊消息"""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            QQ_SEND_MSG_URL.format(openid=openid),
            headers={'Authorization': f'QQBot {token}'},
            json={
                'content': content,
                'msg_type': 0,  # 文本消息
                'msg_id': msg_id or str(uuid.uuid4()),
            }
        )
        return r.status_code == 200


async def _handle_c2c_message(data: dict, token: str):
    """处理 C2C_MESSAGE_CREATE 事件（私聊消息）"""
    author = data.get('author', {})
    qq_openid = author.get('id', '')
    content = data.get('content', '').strip()
    msg_id = data.get('id', '')

    if not qq_openid or not content:
        return

    print(f'[QQ Bot] 收到消息: {qq_openid[:12]}... -> {content[:50]}')

    async with AsyncSessionLocal() as db:
        try:
            # 查找订阅用户（通过 qq_openid）
            result = await db.execute(
                select(Subscriber).options(selectinload(Subscriber.plan))
                .where(Subscriber.qq_openid == qq_openid)
            )
            sub = result.scalar_one_or_none()

            # 未绑定 QQ → 生成绑定码
            if not sub:
                # 检查是否已有未使用的绑定码
                result = await db.execute(
                    select(QQBindCode).where(
                        QQBindCode.qq_openid == qq_openid,
                        QQBindCode.used == False,
                    ).order_by(QQBindCode.created_at.desc())
                )
                existing = result.scalar()
                if existing:
                    code = existing.code
                else:
                    import random
                    code = str(random.randint(100000, 999999))
                    db.add(QQBindCode(qq_openid=qq_openid, code=code))
                    await db.commit()

                await _send_qq_message(qq_openid,
                    QQ_NEED_BIND.replace('{code}', code), token, msg_id)
                return

            # 已过期
            if sub.status != SubscriberStatus.ACTIVE:
                await _send_qq_message(qq_openid, QQ_NOT_SUBSCRIBED, token, msg_id)
                return

            # 配额检查
            await _check_quota(sub, db)
            if sub.messages_limit > 0 and sub.messages_used >= sub.messages_limit:
                await _send_qq_message(qq_openid,
                    QQ_QUOTA_EXHAUSTED.replace('{limit}', str(sub.messages_limit)),
                    token, msg_id)
                return

            # 获取或创建对话
            result = await db.execute(
                select(ChatConversation).where(
                    ChatConversation.subscriber_id == sub.id,
                    ChatConversation.is_active == True,
                ).order_by(ChatConversation.last_message_at.desc())
            )
            conv = result.scalar()
            if not conv:
                conv = ChatConversation(subscriber_id=sub.id, messages=[])
                db.add(conv)
                await db.flush()

            # AI 回复
            nickname = sub.nickname or '用户'
            ai_text = await ai_chat(nickname, conv.messages or [], content)

            # 保存对话
            if not conv.messages:
                conv.messages = []
            conv.messages.append({
                'role': 'user', 'content': content,
                'ts': datetime.now().isoformat()
            })
            conv.messages.append({
                'role': 'assistant', 'content': ai_text,
                'ts': datetime.now().isoformat()
            })
            conv.message_count += 2
            conv.last_message_at = datetime.now()
            sub.messages_used += 1
            sub.total_messages += 1
            await db.commit()

            # 额度预警
            if sub.messages_limit > 0:
                remaining = sub.messages_limit - sub.messages_used
                if 0 < remaining <= max(10, sub.messages_limit * 0.1):
                    ai_text += f'\n\n💡 本月还剩 {remaining} 条'

            # 发送回复
            ok = await _send_qq_message(qq_openid, ai_text, token, msg_id)
            print(f'[QQ Bot] 回复 {"成功" if ok else "失败"}: {ai_text[:50]}')

        except Exception as e:
            print(f'[QQ Bot] 处理消息异常: {e}')
            traceback.print_exc()
            try:
                await _send_qq_message(qq_openid,
                    '抱歉，处理你的消息时出了点问题，请再试一次。', token, msg_id)
            except Exception:
                pass


async def _check_quota(sub, db):
    """月度配额重置"""
    today = date.today()
    if sub.last_reset_at and sub.last_reset_at.month != today.month:
        sub.messages_used = 0
        sub.last_reset_at = today
        await db.commit()


async def _heartbeat(ws, interval_ms: int):
    """发送心跳"""
    interval_s = interval_ms / 1000.0
    seq = 0
    while True:
        await asyncio.sleep(interval_s)
        try:
            seq += 1
            await ws.send(json.dumps({'op': 1, 'd': seq}))
        except Exception:
            break


async def run_qq_bot():
    """QQ Bot 主循环"""
    if not QQ_BOT_ENABLED:
        print('[QQ Bot] 未配置 QQ_BOT_APPID / QQ_BOT_APPSECRET，跳过启动')
        return

    print('[QQ Bot] 启动中...')
    delay = 1

    while True:
        try:
            # 1. 获取 token
            token = await _get_access_token()
            print('[QQ Bot] Access token 获取成功')

            # 2. 获取 WebSocket 地址
            ws_url = await _get_ws_url(token)
            print(f'[QQ Bot] 连接 WebSocket: {ws_url[:60]}...')

            # 3. 连接 WebSocket
            async with websockets.connect(ws_url, max_size=2**20) as ws:
                print('[QQ Bot] WebSocket 已连接')
                delay = 1  # 连接成功，重置重连延迟

                heartbeat_task = None

                async for msg_str in ws:
                    msg = json.loads(msg_str)
                    op = msg.get('op')
                    payload = msg.get('d', {})

                    if op == 10:  # HELLO
                        interval = payload.get('heartbeat_interval', 41250)
                        heartbeat_task = asyncio.create_task(_heartbeat(ws, interval))

                        # 发送 IDENTIFY
                        identify = {
                            'op': 2,
                            'd': {
                                'token': f'QQBot {token}',
                                'intents': 1 << 25,  # C2C_MESSAGE_CREATE
                                'shard': [0, 1],
                                'properties': {}
                            }
                        }
                        await ws.send(json.dumps(identify))
                        print('[QQ Bot] IDENTIFY 已发送')

                    elif op == 0:  # DISPATCH
                        event_type = msg.get('t', '')
                        if event_type == 'C2C_MESSAGE_CREATE':
                            asyncio.create_task(_handle_c2c_message(payload, token))
                        elif event_type == 'READY':
                            print('[QQ Bot] READY — 可以接收消息')

                if heartbeat_task:
                    heartbeat_task.cancel()

        except Exception as e:
            print(f'[QQ Bot] 连接断开: {e}')
            traceback.print_exc()

        delay = min(delay * 2, 60)
        print(f'[QQ Bot] {delay}s 后重连...')
        await asyncio.sleep(delay)
