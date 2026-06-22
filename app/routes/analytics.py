"""
享客虾 — 落地页访问 & 转化统计
"""
from datetime import date, datetime
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.models import get_db, PageVisit, Subscriber

router = APIRouter()


@router.post('/visit')
async def track_visit(data: dict, request: Request, db: AsyncSession = Depends(get_db)):
    """埋点：落地页访问"""
    visit = PageVisit(
        openid=data.get('openid'),
        page=data.get('page', 'landing'),
        source=data.get('source'),
        ua=request.headers.get('user-agent', '')[:255],
    )
    # Cloudflare / Nginx 转发客户端IP
    forwarded = request.headers.get('x-forwarded-for', '')
    visit.ip = forwarded.split(',')[0].strip() if forwarded else (request.client.host if request.client else '')
    db.add(visit)
    await db.commit()
    await db.refresh(visit)
    return {'ok': True, 'visit_id': visit.id}


@router.post('/convert')
async def track_conversion(data: dict, db: AsyncSession = Depends(get_db)):
    """标记转化：openid关联最近一次访问"""
    openid = data.get('openid')
    subscriber_id = data.get('subscriber_id')
    if not openid:
        return {'ok': False, 'error': 'missing openid'}
    # 找该openid最近一次未转化访问
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
        await db.commit()
    return {'ok': True, 'converted': bool(visit)}


class VisitStats(BaseModel):
    today_visits: int = 0
    today_unique: int = 0
    total_visits: int = 0
    total_unique: int = 0
    conversion_rate: float = 0.0
    source_breakdown: list = []


@router.get('/stats')
async def visit_stats(db: AsyncSession = Depends(get_db)):
    """落地页访问统计"""
    today = date.today()

    # 总访问
    total = (await db.execute(select(func.count(PageVisit.id)))).scalar() or 0
    total_uniq = (await db.execute(
        select(func.count(func.distinct(PageVisit.openid))).where(PageVisit.openid.isnot(None))
    )).scalar() or 0

    # 今日访问
    today_visits = (await db.execute(
        select(func.count(PageVisit.id)).where(func.date(PageVisit.created_at) == today)
    )).scalar() or 0
    today_uniq = (await db.execute(
        select(func.count(func.distinct(PageVisit.openid)))
        .where(func.date(PageVisit.created_at) == today, PageVisit.openid.isnot(None))
    )).scalar() or 0

    # 转化率（总转化数 / 有openid的访问）
    converted = (await db.execute(
        select(func.count(PageVisit.id)).where(PageVisit.converted == True)
    )).scalar() or 0
    identifiable = (await db.execute(
        select(func.count(PageVisit.id)).where(PageVisit.openid.isnot(None))
    )).scalar() or 1
    conv_rate = round(converted / max(identifiable, 1) * 100, 1)

    # 来源分布
    source_rows = await db.execute(
        select(PageVisit.source, func.count(PageVisit.id))
        .where(PageVisit.source.isnot(None), PageVisit.source != '')
        .group_by(PageVisit.source)
        .order_by(func.count(PageVisit.id).desc())
        .limit(10)
    )
    sources = [{'source': s, 'count': c} for s, c in source_rows.all()]

    return {
        'today_visits': today_visits,
        'today_unique': today_uniq,
        'total_visits': total,
        'total_unique': total_uniq,
        'converted': converted,
        'conversion_rate': conv_rate,
        'sources': sources,
    }
