"""
统一会员 & 虾点 API — 享客虾体系唯一真相源

所有产品（嗨卡/种子卡/Bot）通过此 API 查询/扣减虾点和会员状态。
数据存储在 139 本地的 subscribers + points_pricing + points_transactions 表。
"""
import os
import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel, Field

logger = logging.getLogger("unified")

router = APIRouter(prefix="/api/unified", tags=["统一会员&虾点"])

# ══════════════════════════════════════════════
# Pydantic 模型
# ══════════════════════════════════════════════

class MemberStatusResponse(BaseModel):
    is_member: bool
    plan_name: str = ""
    expires_at: Optional[str] = None
    days_left: int = 0
    xiake_points: int = 0
    total_consumed: int = 0
    origin: str = ""

class PointsBalanceResponse(BaseModel):
    balance: int
    total_earned: int
    total_consumed: int

class PointsSpendRequest(BaseModel):
    openid: str
    action: str  # from points_pricing, e.g. 'ai_song', 'chat'
    product: str = ""  # 'chat' / 'haicard' / 'seed' / 'rvc'
    ref_id: str = ""   # business reference ID
    device_uuid: str = ""

class PointsSpendResponse(BaseModel):
    success: bool
    points_cost: int = 0
    balance_before: int = 0
    balance_after: int = 0
    error: Optional[str] = None

class PointsAwardRequest(BaseModel):
    openid: str
    amount: int
    reason: str = ""
    product: str = ""
    ref_id: str = ""

class PointsAwardResponse(BaseModel):
    success: bool
    new_balance: int = 0

class MemberSyncRequest(BaseModel):
    openid: str
    device_uuid: str = ""
    nickname: str = ""
    avatar_url: str = ""
    origin: str = ""  # 'bot' / 'haicard' / 'seed'

class MemberCreateRequest(BaseModel):
    openid: str
    plan_id: int
    device_uuid: str = ""
    nickname: str = ""
    origin: str = ""
    amount_paid: int = 0  # 分

class PlanResponse(BaseModel):
    id: int
    name: str
    price: int  # 分
    months: int
    points: int  # 赠送虾点

class VerifyRequest(BaseModel):
    openid: str
    action: str  # from points_pricing

class VerifyResponse(BaseModel):
    allowed: bool
    reason: str = ""
    points_cost: int = 0
    balance: int = 0

# ══════════════════════════════════════════════
# 内部函数
# ══════════════════════════════════════════════

async def _get_subscriber(db, openid: str) -> Optional[dict]:
    """根据 openid 查 subscriber"""
    from sqlalchemy import text
    row = (await db.execute(
        text("""
            SELECT s.id, s.openid, s.nickname, s.device_uuid, s.plan_id,
                   s.status, s.expires_at, s.xiake_points,
                   s.total_points_consumed, s.total_points_recharged,
                   s.origin, p.name as plan_name
            FROM subscribers s
            LEFT JOIN plans p ON s.plan_id = p.id
            WHERE s.openid = :oid
        """),
        {"oid": openid}
    )).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "openid": row[1], "nickname": row[2],
        "device_uuid": row[3], "plan_id": row[4], "status": row[5],
        "expires_at": row[6], "xiake_points": row[7],
        "total_consumed": row[8], "total_recharged": row[9],
        "origin": row[10], "plan_name": row[11] or "",
    }

async def _get_points_cost(action: str) -> int:
    """查定价表"""
    from app.models import AsyncSessionLocal
    from sqlalchemy import text
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            text("SELECT points FROM points_pricing WHERE action_key = :ak"),
            {"ak": action}
        )).fetchone()
        return row[0] if row else 0

async def _ensure_subscriber(db, openid: str,
                               device_uuid: str = "", nickname: str = "",
                               origin: str = "") -> int:
    """确保 subscriber 存在，返回 id"""
    from app.models import AsyncSessionLocal
    from sqlalchemy import text
    existing = await _get_subscriber(db, openid)
    if existing:
        return existing["id"]

    # 创建新 subscriber（免费用户，无会员）
    result = await db.execute(
        text("""
            INSERT INTO subscribers (openid, device_uuid, nickname, origin,
                                      status, xiake_points, plan_id,
                                      started_at, expires_at, created_at)
            VALUES (:oid, :du, :nn, :org, 'ACTIVE', 0, 1,
                    CURRENT_DATE, CURRENT_DATE, NOW())
            ON CONFLICT (openid) DO UPDATE SET
                device_uuid = COALESCE(NULLIF(:du, ''), subscribers.device_uuid),
                nickname = COALESCE(NULLIF(:nn, ''), subscribers.nickname)
            RETURNING id
        """),
        {"oid": openid, "du": device_uuid, "nn": nickname, "org": origin or "api"}
    )
    await db.commit()
    return result.scalar()

# ── 延迟导入（避免启动时 models.py 未初始化） ──
async def _get_db():
    """获取数据库 session（延迟导入）"""
    from app.models import AsyncSessionLocal
    from sqlalchemy.ext.asyncio import AsyncSession
    return AsyncSessionLocal()

def _text():
    """延迟导入 sqlalchemy.text"""
    from sqlalchemy import text as _t
    return _t

# ══════════════════════════════════════════════
# API 端点
# ══════════════════════════════════════════════

@router.get("/member/status", response_model=MemberStatusResponse)
async def get_member_status(openid: str = Query(...)):
    """查询会员状态 + 虾点余额"""
    from app.models import AsyncSessionLocal
    from sqlalchemy import text
    async with AsyncSessionLocal() as db:
        sub = await _get_subscriber(db, openid)
        if not sub:
            return MemberStatusResponse(is_member=False)

        days_left = 0
        expires_at = None
        if sub["expires_at"] and sub["status"] in ("ACTIVE", "TRIAL"):
            if isinstance(sub["expires_at"], date):
                days_left = (sub["expires_at"] - date.today()).days
                expires_at = sub["expires_at"].isoformat()

        return MemberStatusResponse(
            is_member=days_left > 0,
            plan_name=sub["plan_name"],
            expires_at=expires_at,
            days_left=max(0, days_left),
            xiake_points=sub["xiake_points"],
            total_consumed=sub["total_consumed"],
            origin=sub["origin"],
        )

@router.get("/points/balance", response_model=PointsBalanceResponse)
async def get_points_balance(openid: str = Query(...)):
    """查询虾点余额"""
    from app.models import AsyncSessionLocal
    from sqlalchemy import text
    async with AsyncSessionLocal() as db:
        sub = await _get_subscriber(db, openid)
        if not sub:
            return PointsBalanceResponse(balance=0, total_earned=0, total_consumed=0)
        return PointsBalanceResponse(
            balance=sub["xiake_points"],
            total_earned=sub["total_recharged"],
            total_consumed=sub["total_consumed"],
        )

@router.post("/points/spend", response_model=PointsSpendResponse)
async def spend_points(req: PointsSpendRequest):
    """扣减虾点（原子操作）"""
    cost = await _get_points_cost(req.action)
    if cost <= 0:
        return PointsSpendResponse(success=False, error=f"未知动作: {req.action}")

    from app.models import AsyncSessionLocal
    from sqlalchemy import text
    async with AsyncSessionLocal() as db:
        # 确保用户存在
        sub_id = await _ensure_subscriber(db, req.openid, req.device_uuid)

        # 原子扣减：UPDATE ... WHERE balance >= cost
        result = await db.execute(
            text("""
                UPDATE subscribers
                SET xiake_points = xiake_points - :cost,
                    total_points_consumed = total_points_consumed + :cost
                WHERE id = :sid AND xiake_points >= :cost2
                RETURNING xiake_points, total_points_consumed
            """),
            {"cost": cost, "sid": sub_id, "cost2": cost}
        )
        row = result.fetchone()

        if not row:
            # 余额不足，返回当前余额
            sub = await _get_subscriber(db, req.openid)
            return PointsSpendResponse(
                success=False,
                error=f"虾点不足，需要{cost}点，当前{sub['xiake_points']}点",
                balance_before=sub["xiake_points"], balance_after=sub["xiake_points"],
                points_cost=cost,
            )

        balance_after = row[0]
        balance_before = balance_after + cost

        # 记录交易
        await db.execute(
            text("""
                INSERT INTO points_transactions
                    (subscriber_id, amount, balance_after, tx_type, description, created_at)
                VALUES (:sid, :amt, :bal, :act, :desc, NOW())
            """),
            {
                "sid": sub_id,
                "amt": -cost,
                "bal": balance_after,
                "act": req.action,
                "desc": f"{req.product}/{req.ref_id}" if req.product else req.action,
            }
        )
        await db.commit()

        return PointsSpendResponse(
            success=True, points_cost=cost,
            balance_before=balance_before, balance_after=balance_after,
        )

@router.post("/points/award", response_model=PointsAwardResponse)
async def award_points(req: PointsAwardRequest):
    """奖励虾点"""
    if req.amount <= 0:
        return PointsAwardResponse(success=False, new_balance=0)

    from app.models import AsyncSessionLocal
    from sqlalchemy import text
    async with AsyncSessionLocal() as db:
        sub_id = await _ensure_subscriber(db, req.openid)

        result = await db.execute(
            text("""
                UPDATE subscribers
                SET xiake_points = xiake_points + :amt,
                    total_points_recharged = total_points_recharged + :amt
                WHERE id = :sid
                RETURNING xiake_points
            """),
            {"amt": req.amount, "sid": sub_id}
        )
        new_balance = result.scalar()

        await db.execute(
            text("""
                INSERT INTO points_transactions
                    (subscriber_id, amount, balance_after, tx_type, description, created_at)
                VALUES (:sid, :amt, :bal, 'award', :desc, NOW())
            """),
            {
                "sid": sub_id, "amt": req.amount,
                "bal": new_balance,
                "desc": req.reason or f"奖励({req.product})",
            }
        )
        await db.commit()

        return PointsAwardResponse(success=True, new_balance=new_balance)

@router.post("/member/sync")
async def sync_member(req: MemberSyncRequest):
    """同步会员信息（产品侧 → 139）"""
    from app.models import AsyncSessionLocal
    from sqlalchemy import text
    async with AsyncSessionLocal() as db:
        sub_id = await _ensure_subscriber(
            db, req.openid, req.device_uuid,
            req.nickname, req.origin
        )
        return {"success": True, "subscriber_id": sub_id}

@router.get("/member/verify", response_model=VerifyResponse)
async def verify_before_spend(
    openid: str = Query(...),
    action: str = Query(...)
):
    """验证是否有足够虾点执行动作（不扣减，仅检查）"""
    cost = await _get_points_cost(action)
    if cost <= 0:
        return VerifyResponse(allowed=False, reason=f"未知动作: {action}")

    from app.models import AsyncSessionLocal
    from sqlalchemy import text
    async with AsyncSessionLocal() as db:
        sub = await _get_subscriber(db, openid)
        if not sub:
            return VerifyResponse(
                allowed=False, reason="未注册用户",
                points_cost=cost, balance=0
            )

        if sub["xiake_points"] >= cost:
            return VerifyResponse(
                allowed=True, points_cost=cost,
                balance=sub["xiake_points"]
            )
        else:
            return VerifyResponse(
                allowed=False,
                reason=f"虾点不足，需要{cost}点，当前{sub['xiake_points']}点",
                points_cost=cost, balance=sub["xiake_points"]
            )

@router.get("/plans", response_model=list[PlanResponse])
async def list_plans():
    """列出可用套餐"""
    from app.models import AsyncSessionLocal
    from sqlalchemy import text
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            text("""
                SELECT id, name, price, months,
                       CASE
                           WHEN months >= 12 THEN price / 100 * 50  -- 年卡 虾点
                           ELSE price / 100 * 50   -- 月卡 虾点
                       END as points
                FROM plans
                WHERE is_active = true
                ORDER BY sort_order
            """)
        )).fetchall()

        return [
            PlanResponse(id=r[0], name=r[1], price=r[2], months=r[3], points=r[4])
            for r in rows
        ]

@router.post("/member/create")
async def create_member(req: MemberCreateRequest):
    """开通会员：创建 subscriber + 赠送虾点（支付回调调此接口）"""
    from app.models import AsyncSessionLocal
    from sqlalchemy import text
    async with AsyncSessionLocal() as db:
        # 查套餐
        plan = (await db.execute(
            text("SELECT id, name, price, months FROM plans WHERE id = :pid AND is_active = true"),
            {"pid": req.plan_id}
        )).fetchone()
        if not plan:
            raise HTTPException(404, "套餐不存在")

        plan_price, plan_months = plan[2], plan[3]
        points_grant = plan_price // 100 * 50  # 1:50

        # 确保 subscriber 存在
        sub_id = await _ensure_subscriber(db, req.openid, req.device_uuid,
                                            req.nickname, req.origin)

        # 叠加会员有效期
        now = date.today()
        current_expires = (await db.execute(
            text("SELECT expires_at FROM subscribers WHERE id = :sid"),
            {"sid": sub_id}
        )).scalar()

        base = max(now, current_expires or now)
        new_expires = base + timedelta(days=plan_months * 30)

        # 更新会员 + 赠送虾点
        await db.execute(
            text("""
                UPDATE subscribers
                SET plan_id = :pid,
                    status = 'ACTIVE',
                    started_at = CURRENT_DATE,
                    expires_at = :exp,
                    xiake_points = xiake_points + :pts,
                    total_points_recharged = total_points_recharged + :pts,
                    origin = CASE WHEN :org != '' THEN :org ELSE origin END
                WHERE id = :sid
            """),
            {
                "pid": req.plan_id, "exp": new_expires,
                "pts": points_grant, "sid": sub_id,
                "org": req.origin,
            }
        )

        # 记录交易
        await db.execute(
            text("""
                INSERT INTO points_transactions
                    (subscriber_id, amount, balance_after, tx_type, description, created_at)
                VALUES (:sid, :amt,
                    (SELECT xiake_points FROM subscribers WHERE id = :sid2),
                    'subscription', :desc, NOW())
            """),
            {
                "sid": sub_id, "amt": points_grant,
                "sid2": sub_id,
                "desc": f"开通{plan[1]}({req.amount_paid}分)",
            }
        )
        await db.commit()

        return {
            "success": True,
            "subscriber_id": sub_id,
            "plan_name": plan[1],
            "expires_at": new_expires.isoformat(),
            "points_granted": points_grant,
        }
