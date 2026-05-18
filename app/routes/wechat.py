"""
享客虾 — 微信消息回调
v0.1：必须付费才能用
"""
import os
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, date
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
import httpx

from app.models import get_db, Subscriber, ChatConversation, Plan, SubscriberStatus

router = APIRouter()

WECHAT_TOKEN = os.getenv('WECHAT_TOKEN', 'xiaolongxia_wechat_2026')
DEEPSEEK_KEY = os.getenv('DEEPSEEK_API_KEY', '')
DEEPSEEK_URL = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com') + '/v1/chat/completions'
PRODUCT_URL = os.getenv('BASE_URL', 'http://xkx.pangoozn.com')


def verify_signature(sig: str, ts: str, nonce: str) -> bool:
    return hashlib.sha1(''.join(sorted([WECHAT_TOKEN, ts, nonce])).encode()).hexdigest() == sig

def parse_xml(xml_str: str) -> dict:
    root = ET.fromstring(xml_str)
    return {child.tag: child.text for child in root}

def build_text(to_user: str, from_user: str, content: str) -> str:
    return f"""<xml>
<ToUserName><![CDATA[{to_user}]]></ToUserName>
<FromUserName><![CDATA[{from_user}]]></FromUserName>
<CreateTime>{int(datetime.utcnow().timestamp())}</CreateTime>
<MsgType><![CDATA[text]]></MsgType>
<Content><![CDATA[{content}]]></Content>
</xml>"""


async def ai_chat(nickname: str, messages: list, user_msg: str) -> str:
    """AI 回复"""
    if not DEEPSEEK_KEY:
        return '服务暂不可用。'

    system = (
        f'你是{nickname}的私人AI秘书"享客虾"。友好、贴心、高效。'
        f'回复简洁实用，200字以内。直接给答案，不废话。'
    )
    ctx = [{'role': 'system', 'content': system}]
    for m in (messages or [])[-10:]:
        ctx.append({'role': m.get('role', 'user'), 'content': m.get('content', '')[:500]})
    ctx.append({'role': 'user', 'content': user_msg[:2000]})

    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post(DEEPSEEK_URL,
                headers={'Authorization': f'Bearer {DEEPSEEK_KEY}', 'Content-Type': 'application/json'},
                json={'model': 'deepseek-chat', 'messages': ctx, 'max_tokens': 500, 'temperature': 0.7})
            if r.status_code == 200:
                return r.json()['choices'][0]['message']['content'].strip()
            return '（响应失败，请再试一次）'
    except Exception:
        return '（网络波动，请再发一次）'


async def check_quota(sub: Subscriber, db: AsyncSession):
    """月度配额重置"""
    today = date.today()
    if sub.last_reset_at and sub.last_reset_at.month != today.month:
        sub.messages_used = 0
        sub.last_reset_at = today
        await db.commit()


NOT_SUBSCRIBED_MSG = (
    '🦞 嗨！我是**享客虾**，你的私人AI秘书。\n\n'
    '📱 开通后即可开始对话：\n'
    '🥉 基础版 · ¥9.9/月 · 500条\n'
    '🥈 标准版 · ¥19.9/月 · 2000条\n\n'
    '👉 <a href="{url}">点击开通享客虾</a>'
).replace('{url}', PRODUCT_URL)

QUOTA_EXHAUSTED_MSG = (
    '🦞 本月 {limit} 条额度已用完。\n\n'
    '👉 <a href="{url}">续费或升级套餐</a>'
).replace('{url}', PRODUCT_URL)


# ===== 回调 =====

@router.get('/callback')
async def verify(signature='', timestamp='', nonce='', echostr=''):
    if verify_signature(signature, timestamp, nonce):
        return PlainTextResponse(echostr)
    raise HTTPException(403)


@router.post('/callback')
async def callback(request: Request, db: AsyncSession = Depends(get_db)):
    params = dict(request.query_params)
    if not verify_signature(params.get('signature',''), params.get('timestamp',''), params.get('nonce','')):
        raise HTTPException(403)

    body = await request.body()
    xml = parse_xml(body.decode('utf-8'))
    msg_type = xml.get('MsgType', '')
    from_user = xml.get('FromUserName', '')
    to_user = xml.get('ToUserName', '')

    # 关注事件
    if msg_type == 'event' and xml.get('Event') == 'subscribe':
        return PlainTextResponse(build_text(from_user, to_user, NOT_SUBSCRIBED_MSG))

    # 文本消息
    if msg_type == 'text':
        content = xml.get('Content', '').strip()

        # 快捷指令
        if content in ('开通', '升级', '套餐', '续费'):
            return PlainTextResponse(build_text(from_user, to_user, NOT_SUBSCRIBED_MSG))

        # 找订阅用户
        result = await db.execute(
            select(Subscriber).options(selectinload(Subscriber.plan))
            .where(Subscriber.openid == from_user)
        )
        sub = result.scalar_one_or_none()

        # 未订阅 → 引导付费
        if not sub or sub.status != SubscriberStatus.ACTIVE:
            return PlainTextResponse(build_text(from_user, to_user, NOT_SUBSCRIBED_MSG))

        # 配额检查
        await check_quota(sub, db)
        if sub.messages_limit > 0 and sub.messages_used >= sub.messages_limit:
            return PlainTextResponse(build_text(from_user, to_user,
                QUOTA_EXHAUSTED_MSG.replace('{limit}', str(sub.messages_limit))))

        # 对话
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

        ai_text = await ai_chat(sub.nickname, conv.messages or [], content)

        if not conv.messages:
            conv.messages = []
        conv.messages.append({'role': 'user', 'content': content, 'ts': datetime.utcnow().isoformat()})
        conv.messages.append({'role': 'assistant', 'content': ai_text, 'ts': datetime.utcnow().isoformat()})
        conv.message_count += 2
        conv.last_message_at = datetime.utcnow()
        sub.messages_used += 1
        sub.total_messages += 1
        await db.commit()

        # 余额不足提醒
        if sub.messages_limit > 0:
            remaining = sub.messages_limit - sub.messages_used
            if 0 < remaining <= sub.messages_limit * 0.1:
                ai_text += f'\n\n—\n💡 本月还剩 {remaining} 条，<a href="{PRODUCT_URL}">续费</a>'

        return PlainTextResponse(build_text(from_user, to_user, ai_text))

    return PlainTextResponse('success')


# ===== Mock 测试 =====

class MockChat(BaseModel):
    openid: str = 'test_paid_user'
    message: str = '你好'

@router.post('/mock-chat')
async def mock_chat(data: MockChat, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Subscriber).options(selectinload(Subscriber.plan))
        .where(Subscriber.openid == data.openid)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        return {'error': '未订阅，请先开通', 'subscribe_url': PRODUCT_URL}
    if sub.status != SubscriberStatus.ACTIVE:
        return {'error': f'订阅状态：{sub.status.value}', 'subscribe_url': PRODUCT_URL}

    await check_quota(sub, db)
    if sub.messages_limit > 0 and sub.messages_used >= sub.messages_limit:
        return {'error': '额度已用完', 'limit': sub.messages_limit}

    result = await db.execute(
        select(ChatConversation).where(
            ChatConversation.subscriber_id == sub.id, ChatConversation.is_active == True
        ).order_by(ChatConversation.last_message_at.desc())
    )
    conv = result.scalar()
    if not conv:
        conv = ChatConversation(subscriber_id=sub.id, messages=[])
        db.add(conv)
        await db.flush()

    ai_text = await ai_chat(sub.nickname, conv.messages or [], data.message)
    conv.messages.append({'role': 'user', 'content': data.message, 'ts': datetime.utcnow().isoformat()})
    conv.messages.append({'role': 'assistant', 'content': ai_text, 'ts': datetime.utcnow().isoformat()})
    conv.message_count += 2
    conv.last_message_at = datetime.utcnow()
    sub.messages_used += 1
    sub.total_messages += 1
    await db.commit()

    return {
        'reply': ai_text,
        'subscriber_id': sub.id, 'nickname': sub.nickname,
        'status': sub.status.value, 'messages_used': sub.messages_used,
        'messages_limit': sub.messages_limit,
    }
