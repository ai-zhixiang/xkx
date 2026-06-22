"""
享客虾 — 文件上传 API
"""
import json
import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, Form, Request, Depends, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import get_db, Subscriber, ChatConversation, SubscriberStatus
from app.services.file_handler import save_and_extract, UPLOAD_DIR

router = APIRouter()

# 待注入的上下文文件：{openid: extracted_text}
_pending_context = {}

UPLOAD_HTML_PATH = Path(os.getenv('UPLOAD_HTML_PATH',
    '/home/ubuntu/weclaw-1/app/templates/upload.html'))


def get_pending_context(openid: str) -> str:
    """获取并清除待注入的上下文"""
    return _pending_context.pop(openid, '')


@router.get('/upload')
async def upload_page():
    """返回 H5 上传页面"""
    if not UPLOAD_HTML_PATH.exists():
        return HTMLResponse('<h2>上传页面加载中…</h2>')
    return HTMLResponse(UPLOAD_HTML_PATH.read_text(encoding='utf-8'))


@router.post('/api/upload')
async def upload_file(
    file: UploadFile = File(...),
    openid: str = Form(''),
    db: AsyncSession = Depends(get_db),
):
    """接收文件上传"""
    # 校验 openid
    if not openid:
        raise HTTPException(400, '缺少用户标识')

    result = await db.execute(
        select(Subscriber).where(
            Subscriber.openid == openid,
            Subscriber.status == SubscriberStatus.ACTIVE,
        )
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(403, '请先在微信中开通享客虾')

    # 读取文件
    content = await file.read()
    if not content:
        raise HTTPException(400, '文件为空')

    # 处理
    filename = file.filename or 'unnamed'
    result = save_and_extract(openid, filename, content)

    if not result['ok']:
        raise HTTPException(400, result['error'])

    # 注入上下文
    extracted = result['extracted_text']
    if extracted:
        context_msg = (
            f'[用户通过文件上传器发送了文件：{filename}]\n'
            f'文件内容如下，请基于此内容回答用户的后续问题：\n\n'
            f'{extracted[:3000]}'
        )
        # 写入当前活跃对话
        conv_result = await db.execute(
            select(ChatConversation).where(
                ChatConversation.subscriber_id == sub.id,
                ChatConversation.is_active == True,
            ).order_by(ChatConversation.last_message_at.desc())
        )
        conv = conv_result.scalar()
        if not conv:
            conv = ChatConversation(subscriber_id=sub.id, messages=[])
            db.add(conv)
            await db.flush()

        if not conv.messages:
            conv.messages = []
        conv.messages.append({
            'role': 'system', 'content': context_msg,
            'ts': datetime.now().isoformat()
        })
        conv.message_count += 1
        conv.last_message_at = datetime.now()
        await db.commit()

    return {
        'ok': True,
        'file_name': result['file_name'],
        'file_type': result['file_type'],
        'file_size': result['file_size'],
        'content_preview': result['content_preview'],
        'message': '文件已接收！内容已注入对话上下文，回到公众号继续聊天即可引用。',
    }
