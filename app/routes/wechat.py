"""
享客虾 — 微信消息回调
v0.1：必须付费才能用
"""
import os
import hashlib
import asyncio
import logging
import tempfile
import subprocess
import xml.etree.ElementTree as ET
import markdown
from datetime import datetime, date, timedelta
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
import httpx

from app.models import get_db, AsyncSessionLocal, Subscriber, ChatConversation, Plan, SubscriberStatus, QQBindCode, PageVisit
from app.services.ai import ai_chat
from app.agent import AgentSessionManager
from app.agent.session_manager import _sessions, AgentSession

logger = logging.getLogger('xkx.wechat')

router = APIRouter()

WECHAT_TOKEN = os.getenv('WECHAT_TOKEN', 'weclawd_wechat_2026')
WX_APPID = os.getenv('WECHAT_APPID', '')
WX_APPSECRET = os.getenv('WECHAT_APPSECRET', '')
PRODUCT_URL = os.getenv('BASE_URL', 'http://xkx.pangoozn.com')

# === access_token 缓存（Redis 共享，与 ailuckycards 共用同一服务号）===

import redis.asyncio as _redis_mod
_token_redis: _redis_mod.Redis | None = None
TOKEN_CACHE_KEY = "wechat:access_token"  # 与 ailuckycards 相同 key
TOKEN_EXPIRE = 7000  # 提前 200s 刷新


def _get_token_redis():
    global _token_redis
    if _token_redis is None:
        _token_redis = _redis_mod.from_url(
            os.getenv('REDIS_URL', 'redis://localhost:6379/0'),
            decode_responses=True
        )
    return _token_redis


async def _get_access_token() -> str:
    """获取微信公众号 access_token（Redis 共享缓存，与 ailuckycards 同步）"""
    r = _get_token_redis()
    cached = await r.get(TOKEN_CACHE_KEY)
    if cached:
        return cached
    
    async with httpx.AsyncClient(timeout=10) as c:
        resp = await c.get(
            f'https://api.weixin.qq.com/cgi-bin/token'
            f'?grant_type=client_credential&appid={WX_APPID}&secret={WX_APPSECRET}'
        )
        data = resp.json()
        token = data.get('access_token', '')
        if token:
            await r.setex(TOKEN_CACHE_KEY, TOKEN_EXPIRE, token)
            logger.info(f'[微信] access_token 刷新成功')
        else:
            logger.error(f'[微信] access_token 获取失败: {data}')
        return token


async def send_customer_message(openid: str, content: str, msgtype: str = 'text'):
    """推送微信客服消息（异步，不抛异常）。
    msgtype: 'text' | 'image' (需传 media_id)
    """
    try:
        token = await _get_access_token()
        if not token:
            return
        if msgtype == 'image':
            media_id = await _upload_image_media(token, content)
            if not media_id:
                return
            body = {'touser': openid, 'msgtype': 'image', 'image': {'media_id': media_id}}
        else:
            body = {'touser': openid, 'msgtype': 'text', 'text': {'content': content}}
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f'https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token={token}',
                json=body
            )
            if r.status_code != 200 or r.json().get('errcode', 0) != 0:
                logger.warning(f'[微信] 客服消息推送失败: {r.text[:200]}')
    except Exception as e:
        logger.error(f'[微信] 客服消息异常: {e}')


async def _upload_image_media(token: str, filepath: str) -> str | None:
    """上传图片到微信临时素材，返回 media_id"""
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            with open(filepath, 'rb') as f:
                r = await c.post(
                    f'https://api.weixin.qq.com/cgi-bin/media/upload?access_token={token}&type=image',
                    files={'media': (os.path.basename(filepath), f, 'image/png')}
                )
            data = r.json()
            media_id = data.get('media_id', '')
            if media_id:
                return media_id
            logger.warning(f'[微信] 图片上传失败: {r.text[:200]}')
            return None
    except Exception as e:
        logger.error(f'[微信] 图片上传异常: {e}')
        return None


def render_markdown_image(md_text: str) -> str | None:
    """Markdown → 精美图片，返回临时文件路径。
    微信图片限制 2MB、宽高最好 600x800 左右。"""
    html_body = markdown.markdown(md_text, extensions=['extra', 'codehilite', 'tables'])
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  body {{ font-family: 'PingFang SC','Microsoft YaHei',sans-serif; font-size:16px; line-height:1.8;
         color:#333; max-width:560px; margin:30px auto; padding:20px; background:#fff; }}
  h1,h2,h3 {{ color:#1a1a2e; margin-top:1.2em; }}
  h1 {{ font-size:22px; border-bottom:2px solid #e94560; padding-bottom:8px; }}
  h2 {{ font-size:19px; }}
  h3 {{ font-size:17px; }}
  p {{ margin:0.8em 0; }}
  ul,ol {{ padding-left:1.5em; }}
  li {{ margin:0.3em 0; }}
  code {{ background:#f4f4f4; padding:2px 6px; border-radius:3px; font-size:14px; }}
  pre {{ background:#1a1a2e; color:#f8f8f2; padding:14px; border-radius:6px; overflow-x:auto; font-size:13px; }}
  blockquote {{ border-left:3px solid #e94560; padding-left:14px; color:#666; margin:1em 0; }}
  table {{ border-collapse:collapse; width:100%; margin:1em 0; }}
  th,td {{ border:1px solid #ddd; padding:8px 12px; text-align:left; }}
  th {{ background:#1a1a2e; color:#fff; }}
  strong {{ color:#e94560; }}
  em {{ color:#555; }}
</style></head><body>{html_body}</body></html>"""
    try:
        fd, path = tempfile.mkstemp(suffix='.html')
        os.write(fd, html.encode('utf-8'))
        os.close(fd)
        img_path = path.replace('.html', '.png')
        subprocess.run(
            ['wkhtmltoimage', '--width', '600', '--quality', '92',
             '--encoding', 'UTF-8', path, img_path],
            check=True, timeout=15, capture_output=True
        )
        os.unlink(path)  # 删掉临时 HTML
        if os.path.exists(img_path) and os.path.getsize(img_path) < 2 * 1024 * 1024:
            return img_path
        logger.warning(f'[微信] 图片生成失败或超 2MB: {img_path}')
        return None
    except Exception as e:
        logger.error(f'[微信] 图片渲染异常: {e}')
        return None


# === 后台 Agent 处理（v0.5.0：两层超时 60s + 60s）===

async def _process_with_agent(
    openid: str, nickname: str, tier: str,
    conv_id: int, user_msg: str, history: list,
):
    """后台用 Agent 处理用户消息。
    两层超时：
      60s — 前台超时，推送「暂无反馈」
      60s — 后台超时，杀 Agent 进程 + 推送错误
    """
    session = None
    try:
        # 从全局池获取或创建会话
        if openid not in _sessions:
            _sessions[openid] = AgentSession(openid, tier)
        session = _sessions[openid]

        # === 第一层：60s 前台 ===
        try:
            result = await asyncio.wait_for(
                session.send(user_msg, history=history, kill_on_timeout=False),
                timeout=60
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # 前台超时 → 推送提示
            await send_customer_message(openid, '🦞 暂无反馈，可以继续下一个话题')
            logger.info(f'[Agent] 前台超时 (60s): {openid[:12]}...')

            # === 第二层：60s 后台 ===
            try:
                result = await asyncio.wait_for(
                    session.wait_pending(),
                    timeout=60
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # 后台也超时 → 杀进程
                logger.warning(f'[Agent] 后台超时 (120s 总计)，杀进程: {openid[:12]}...')
                await session.stop()
                if openid in _sessions:
                    del _sessions[openid]
                await send_customer_message(openid, '🦞 抱歉，AI 引擎暂时无法响应，请稍后再试。')
                return

        # === 成功：推送回复 + 更新DB ===
        if result:
            await send_customer_message(openid, result)
            # 更新对话历史
            async with AsyncSessionLocal() as db:
                conv = (await db.execute(
                    select(ChatConversation).where(ChatConversation.id == conv_id)
                )).scalar_one_or_none()
                if conv:
                    if not conv.messages:
                        conv.messages = []
                    conv.messages.append({'role': 'user', 'content': user_msg, 'ts': datetime.now().isoformat()})
                    conv.messages.append({'role': 'assistant', 'content': result, 'ts': datetime.now().isoformat()})
                    conv.message_count += 2
                    conv.last_message_at = datetime.now()
                    # 更新配额
                    sub = (await db.execute(
                        select(Subscriber).where(Subscriber.openid == openid)
                    )).scalar_one_or_none()
                    if sub:
                        sub.messages_used += 1
                        sub.total_messages += 1
                    await db.commit()
    except Exception as e:
        logger.exception(f'[Agent] 后台处理异常: {e}')
        try:
            await send_customer_message(openid, f'🦞 出了点问题：{str(e)[:50]}')
        except Exception:
            pass


def verify_signature(sig: str, ts: str, nonce: str) -> bool:
    return hashlib.sha1(''.join(sorted([WECHAT_TOKEN, ts, nonce])).encode()).hexdigest() == sig

def parse_xml(xml_str: str) -> dict:
    root = ET.fromstring(xml_str)
    return {child.tag: child.text for child in root}

def build_text(to_user: str, from_user: str, content: str) -> str:
    return f"""<xml>
<ToUserName><![CDATA[{to_user}]]></ToUserName>
<FromUserName><![CDATA[{from_user}]]></FromUserName>
<CreateTime>{int(datetime.now().timestamp())}</CreateTime>
<MsgType><![CDATA[text]]></MsgType>
<Content><![CDATA[{content}]]></Content>
</xml>"""


async def check_quota(sub: Subscriber, db: AsyncSession):
    """月度配额重置"""
    today = date.today()
    if sub.last_reset_at and sub.last_reset_at.month != today.month:
        sub.messages_used = 0
        sub.last_reset_at = today
        await db.commit()


# 微信通道专用文案（含 HTML <a> 链接）
NOT_SUBSCRIBED_MSG = (
    '🦞 嗨！我是**享客虾**，你的私人AI秘书。\n\n'
    '📱 开通后即可开始对话：\n'
    '🥉 基础版 · ¥9.9/月 · 500条\n'
    '🥈 标准版 · ¥19.9/月 · 2000条\n\n'
    '👉 <a href=\"{url}\">点击开通享客虾</a>'
).replace('{url}', PRODUCT_URL)

QUOTA_EXHAUSTED_MSG = (
    '🦞 本月 {limit} 条额度已用完。\n\n'
    '👉 <a href="{url}">续费或升级套餐</a>'
).replace('{url}', PRODUCT_URL)

TRIAL_ENDED_MSG = (
    '🦞 3天试用（100条）已用完～\n\n'
    '想继续使用AI私人秘书？\n'
    '🎉 公测优惠中！\n'
    '🥉 基础版 ¥9.9/月（500条）\n'
    '🥈 标准版 ¥19.9/月（2000条）\n\n'
    '👉 <a href="{url}">立即开通享客虾</a>'
).replace('{url}', PRODUCT_URL)

TRIAL_WELCOME_MSG = (
    '🎉 已为你开通**3天免费试用**！\n\n'
    '🦞 享客虾是你的私人AI秘书，随时为你服务。\n'
    '📝 试用：100条消息 · 3天有效\n\n'
    '试试问我：\n'
    '• 帮我查天气\n'
    '• 写一段祝福语\n'
    '• 帮我规划周末\n\n'
    '💡 试用结束后可升级：\n'
    '🥉 基础版 ¥9.9/月（500条）\n'
    '🥈 标准版 ¥19.9/月（2000条）\n\n'
    '👉 <a href="{url}">查看套餐</a>'
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

        # 找开发团队 / 商务合作 → 直接推 PRO 联系方式
        dev_keywords = ('开发团队', '合作', '商务', '定制', 'PRO', 'pro', '联系开发', '找开发')
        if any(kw in content for kw in dev_keywords):
            return PlainTextResponse(build_text(from_user, to_user,
                '💎 享客虾 PRO 专属定制\n\n'
                '🖥️ 专属服务器 · ⚙️ 安装配置\n'
                '🎓 在线培训 · 🛠️ 技术支持\n'
                '📁 无限文件 · 👨‍💼 1对1服务\n\n'
                '💰 ¥880/年 · ¥200定金\n'
                '⚠️ 不含算力API费用\n\n'
                '👇 添加微信私聊\n'
                '铭道@智享家.AI\n\n'
                '或点击查看详情：\n'
                'https://hai.pangoozn.com/xkx/'))

        # 绑定 QQ 机器人
        if content.startswith('绑定 ') or content.startswith('绑定'):
            code = content.replace('绑定', '').strip()
            if code and len(code) == 6 and code.isdigit():
                result = await db.execute(
                    select(QQBindCode).where(
                        QQBindCode.code == code,
                        QQBindCode.used == False,
                    )
                )
                bind = result.scalar_one_or_none()
                if bind:
                    # 更新当前用户的 qq_openid
                    sub = (await db.execute(
                        select(Subscriber).where(Subscriber.openid == from_user)
                    )).scalar_one_or_none()
                    if sub:
                        sub.qq_openid = bind.qq_openid
                        bind.used = True
                        await db.commit()
                        return PlainTextResponse(build_text(from_user, to_user,
                            '✅ QQ 绑定成功！现在可以在 QQ 上和享客虾聊天了。'))
                    else:
                        return PlainTextResponse(build_text(from_user, to_user,
                            '请先在微信中开通享客虾。'))
                else:
                    return PlainTextResponse(build_text(from_user, to_user,
                        '绑定码无效或已过期，请在 QQ 中重新获取。'))
            else:
                return PlainTextResponse(build_text(from_user, to_user,
                    '请发送「绑定 + 6位数字码」，例如：绑定 123456\n在 QQ 中和享客虾对话即可获取绑定码。'))

        # 找订阅用户
        result = await db.execute(
            select(Subscriber).options(selectinload(Subscriber.plan))
            .where(Subscriber.openid == from_user)
        )
        sub = result.scalar_one_or_none()

        # 未订阅 → 开通试用
        if not sub:
            plan_result = await db.execute(
                select(Plan).where(Plan.is_active == True).order_by(Plan.sort_order).limit(1)
            )
            first_plan = plan_result.scalar_one_or_none()
            if first_plan:
                sub = Subscriber(
                    openid=from_user,
                    plan_id=first_plan.id,
                    status=SubscriberStatus.TRIAL,
                    started_at=date.today(),
                    expires_at=date.today() + timedelta(days=3),
                    messages_limit=100,
                    trial_used=True,
                )
                db.add(sub)
                await db.flush()
                # 转化追踪：标记最近访问为已转化
                await _mark_conversion(db, from_user, sub.id)
                return PlainTextResponse(build_text(from_user, to_user, TRIAL_WELCOME_MSG))

        # 试用中 → 检查到期/额度
        if sub.status == SubscriberStatus.TRIAL:
            if date.today() > sub.expires_at:
                return PlainTextResponse(build_text(from_user, to_user, TRIAL_ENDED_MSG))
            await check_quota(sub, db)
            if sub.messages_limit > 0 and sub.messages_used >= sub.messages_limit:
                return PlainTextResponse(build_text(from_user, to_user, TRIAL_ENDED_MSG))

        # 未激活 → 引导付费（试用已在上方处理）
        if sub.status != SubscriberStatus.ACTIVE:
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

        # 扣配额 + 提交（先记账，Agent 回复后补对话历史）
        sub.messages_used += 1
        sub.total_messages += 1
        await db.commit()

        # === v0.5.0: Agent 异步处理 ===
        plan_name = sub.plan.name if sub.plan else ''
        tier = 'standard' if '标准' in plan_name else 'pro' if '专业' in plan_name else 'basic'
        asyncio.create_task(_process_with_agent(
            openid=from_user,
            nickname=sub.nickname,
            tier=tier,
            conv_id=conv.id,
            user_msg=content,
            history=conv.messages or [],
        ))

        return PlainTextResponse('success')

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
    conv.messages.append({'role': 'user', 'content': data.message, 'ts': datetime.now().isoformat()})
    conv.messages.append({'role': 'assistant', 'content': ai_text, 'ts': datetime.now().isoformat()})
    conv.message_count += 2
    conv.last_message_at = datetime.now()
    sub.messages_used += 1
    sub.total_messages += 1
    await db.commit()

    return {
        'reply': ai_text,
        'subscriber_id': sub.id, 'nickname': sub.nickname,
        'status': sub.status.value, 'messages_used': sub.messages_used,
        'messages_limit': sub.messages_limit,
    }


# === 服务号关注二维码（缓存永久 ticket）===

_qr_ticket_cache: dict = {'ticket': '', 'url': ''}


@router.get('/follow-qrcode')
async def get_follow_qrcode():
    """返回智享家服务号关注二维码 URL（带缓存）"""
    if _qr_ticket_cache['url']:
        return {'qr_url': _qr_ticket_cache['url']}
    token = await _get_access_token()
    if not token:
        return {'error': 'token 获取失败'}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f'https://api.weixin.qq.com/cgi-bin/qrcode/create?access_token={token}',
                json={'action_name': 'QR_LIMIT_STR_SCENE',
                      'action_info': {'scene': {'scene_str': 'follow_xkx'}}}
            )
            data = r.json()
            ticket = data.get('ticket', '')
            if ticket:
                url = f'https://mp.weixin.qq.com/cgi-bin/showqrcode?ticket={ticket}'
                _qr_ticket_cache['ticket'] = ticket
                _qr_ticket_cache['url'] = url
                return {'qr_url': url}
            errmsg = data.get("errmsg", "")
            return {'error': f'创建失败: {errmsg}'}
    except Exception as e:
        return {'error': str(e)}

# ===== 转化追踪辅助 =====

async def _mark_conversion(db: AsyncSession, openid: str, subscriber_id: int):
    """标记落地页访问为已转化"""
    try:
        result = await db.execute(
            select(PageVisit).where(
                PageVisit.openid == openid,
                PageVisit.converted == False
            ).order_by(PageVisit.created_at.desc()).limit(1)
        )
        visit = result.scalar_one_or_none()
        if visit:
            visit.converted = True
            visit.converted_at = datetime.now()
            visit.subscriber_id = subscriber_id
            await db.flush()
    except Exception:
        pass  # 转化统计失败不影响主流程
