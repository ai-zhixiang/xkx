"""Push API — 全员推送服务（每日精读/通知推送）"""

import logging
import json
import os
import time
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/push", tags=["push"])


class DailyArticlePush(BaseModel):
    title: str
    digest: str
    url: str
    tag: str = ""


@router.post("/daily-article")
async def push_daily_article(req: DailyArticlePush):
    """每日精读发布后，推送给所有订阅用户"""
    from app.models import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""SELECT id, openid, nickname FROM subscribers
               WHERE daily_push = true AND openid IS NOT NULL""")
        )
        subs = result.fetchall()

    if not subs:
        return {"pushed": 0, "message": "暂无订阅用户"}

    # 写入 notifications 表
    async with AsyncSessionLocal() as session:
        for sub in subs:
            await session.execute(
                text("""INSERT INTO notifications (category, title, content, subscriber_id, is_read)
                   VALUES (:cat, :title, :content, :sid, false)"""),
                {
                    "cat": "daily_article",
                    "title": req.title,
                    "content": f"{req.digest}\n\n{req.url}\n#阅读原文\n来源: 享客虾评",
                    "sid": sub.id,
                }
            )
        await session.commit()

    # 写 pending_push 文件供 keepalive 自检推送
    import datetime
    push_record = {
        "title": req.title,
        "digest": req.digest,
        "url": req.url,
        "tag": req.tag,
        "ts": datetime.datetime.now().isoformat(),
    }
    os.makedirs("/home/ubuntu/stock-quant", exist_ok=True)
    with open("/home/ubuntu/stock-quant/pending_push.json", "w", encoding="utf-8") as f:
        json.dump(push_record, f, ensure_ascii=False)

    return {
        "pushed": len(subs),
        "success": len(subs),
        "title": req.title,
    }
