"""
享客虾 — 文档管理 API
核心功能：AI生成文档 / 列表 / 下载 / 删除
"""
import os
import hashlib
from datetime import datetime, date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.models import get_db, Subscriber, Document

router = APIRouter()

OUTPUT_DIR = Path(__file__).parent.parent / 'static' / 'output'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
PDF_API_URL = os.getenv('XKX_PDF_API', 'http://localhost:8001/api/generate/pdf')

DEEPSEEK_KEY = os.getenv('DEEPSEEK_API_KEY', 'os.getenv("DEEPSEEK_API_KEY")')
DEEPSEEK_URL = 'https://api.deepseek.com/v1/chat/completions'


class DocGenerateRequest(BaseModel):
    title: str
    prompt: str
    category: str = '其他'
    subscriber_id: int = None  # 可选，用于 WeChat 回调


async def generate_content(prompt: str) -> str:
    """调用 DeepSeek 生成文档内容"""
    import httpx
    system = """你是「享客虾」的高级商业分析师。撰写文档时：
- 结构清晰：引言 → 展开 → 案例 → 总结
- 故事化叙事，有画面感，有具体人物/年代/案例
- 至少1500字
- Markdown格式：## 标题、- 列表、**加粗**、> 引用"""
    
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(DEEPSEEK_URL,
            headers={'Authorization': f'Bearer {DEEPSEEK_KEY}', 'Content-Type': 'application/json'},
            json={
                'model': 'deepseek-chat',
                'messages': [
                    {'role': 'system', 'content': system},
                    {'role': 'user', 'content': prompt}
                ],
                'max_tokens': 4096, 'temperature': 0.7
            })
        if r.status_code != 200:
            raise Exception(f"DeepSeek error: {r.status_code}")
        return r.json()['choices'][0]['message']['content'].strip()


async def render_pdf(title: str, subtitle: str, content: str, author: str = '享客虾AI秘书') -> dict:
    """调用 PDF 引擎渲染"""
    import httpx
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(PDF_API_URL, json={
            'title': title, 'subtitle': subtitle or '',
            'content': content, 'author': author
        })
        if r.status_code == 200:
            data = r.json()
            if data.get('ok'):
                return data
        raise Exception(f"PDF render failed: {r.text[:200]}")


# ===== API =====

@router.get('/documents')
async def list_documents(
    subscriber_id: int = None,
    category: str = None,
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db)
):
    """列出文档（可按用户/分类筛选）"""
    q = select(Document).where(Document.is_deleted == False)
    if subscriber_id:
        q = q.where(Document.subscriber_id == subscriber_id)
    if category:
        q = q.where(Document.category == category)
    q = q.order_by(Document.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    
    result = await db.execute(q)
    docs = result.scalars().all()
    
    total_q = select(func.count(Document.id)).where(Document.is_deleted == False)
    if subscriber_id:
        total_q = total_q.where(Document.subscriber_id == subscriber_id)
    if category:
        total_q = total_q.where(Document.category == category)
    total = (await db.execute(total_q)).scalar()
    
    return {
        'docs': [{
            'id': d.id, 'title': d.title, 'subtitle': d.subtitle,
            'summary': d.summary, 'category': d.category,
            'file_size': d.file_size,
            'file_url': f'https://hai.pangoozn.com/xkx/static/output/{d.file_path}' if d.file_path else None,
            'created_at': d.created_at.isoformat() if d.created_at else None,
        } for d in docs],
        'total': total, 'page': page, 'page_size': page_size
    }


@router.post('/documents/generate')
async def generate_document(data: DocGenerateRequest, db: AsyncSession = Depends(get_db)):
    """AI 生成文档 + 存库 + 返回下载链接"""
    # 1. AI 写内容
    content = await generate_content(data.prompt)
    
    # 2. 生成摘要
    summary = content[:200].replace('\n', ' ').replace('#', '').strip()
    
    # 3. PDF 渲染
    pdf_result = await render_pdf(data.title, '', content)
    
    # 4. 提取文件名
    file_url = pdf_result.get('url', '')
    file_name = file_url.rsplit('/', 1)[-1] if file_url else ''
    
    # 5. 存库
    doc = Document(
        subscriber_id=data.subscriber_id,
        title=data.title,
        summary=summary,
        category=data.category,
        file_path=file_name,
        file_size=pdf_result.get('size', 0),
        content_text=content,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    
    download_url = f'https://hai.pangoozn.com/xkx/static/output/{file_name}'
    
    return {
        'ok': True,
        'id': doc.id,
        'title': doc.title,
        'summary': summary,
        'download_url': download_url,
        'file_size': pdf_result.get('size', 0),
    }


@router.get('/documents/{doc_id}')
async def get_document(doc_id: int, db: AsyncSession = Depends(get_db)):
    """获取单个文档详情"""
    result = await db.execute(
        select(Document).where(Document.id == doc_id, Document.is_deleted == False)
    )
    doc = result.scalar()
    if not doc:
        raise HTTPException(404, '文档不存在')
    
    return {
        'id': doc.id, 'title': doc.title, 'subtitle': doc.subtitle,
        'summary': doc.summary, 'category': doc.category,
        'content_text': doc.content_text,
        'file_url': f'https://hai.pangoozn.com/xkx/static/output/{doc.file_path}' if doc.file_path else None,
        'file_size': doc.file_size,
        'created_at': doc.created_at.isoformat() if doc.created_at else None,
    }


@router.delete('/documents/{doc_id}')
async def delete_document(doc_id: int, db: AsyncSession = Depends(get_db)):
    """软删除文档"""
    result = await db.execute(select(Document).where(Document.id == doc_id))
    doc = result.scalar()
    if not doc:
        raise HTTPException(404, '文档不存在')
    doc.is_deleted = True
    await db.commit()
    return {'ok': True}
