"""
🦞 享客虾 · 资源管理 API
虾点/磁盘/用量查询
"""
import logging
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.models import get_db
from app.bot.resources import (
    get_member_points_async, check_and_deduct_async, add_points_async,
    get_usage_stats_async, admin_all_users_resources,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/resources", tags=["resources"])


# ── 请求/响应模型 ──

class BalanceResponse(BaseModel):
    ok: bool = True
    xiake_points: int = 0
    points_expires_at: str = ""
    is_member: bool = False

class ConsumeRequest(BaseModel):
    openid: str
    action: str = "chat"
    points: int | None = None

class ConsumeResponse(BaseModel):
    ok: bool
    remaining: int = 0
    need: int = 0
    message: str = ""

class RechargeRequest(BaseModel):
    openid: str
    amount: int
    description: str = ""

class RechargeResponse(BaseModel):
    ok: bool
    balance_after: int = 0

class UsageResponse(BaseModel):
    ok: bool = True
    points_balance: int = 0
    points_consumed_today: int = 0
    points_consumed_month: int = 0
    recharged_total: int = 0
    points_expires_at: str = ""
    disk_used_mb: float = 0
    disk_quota_mb: float = 2048
    chat_count_today: int = 0
    is_member: bool = False
    expires_at: str = ""


# ── 端点 ──

@router.get("/balance", response_model=BalanceResponse)
async def get_balance(openid: str = Query(""), db=Depends(get_db)):
    """查虾点余额"""
    info = await get_member_points_async(db, openid)
    return BalanceResponse(
        xiake_points=info["xiake_points"],
        points_expires_at=info.get("points_expires_at") or "",
        is_member=info["is_member"],
    )


@router.post("/consume", response_model=ConsumeResponse)
async def consume_points(req: ConsumeRequest, db=Depends(get_db)):
    """扣虾点（内部调用）"""
    return await check_and_deduct_async(db, req.openid, req.action, req.points)


@router.post("/recharge", response_model=RechargeResponse)
async def recharge_points(req: RechargeRequest, db=Depends(get_db)):
    """加虾点（支付成功后调用）"""
    return await add_points_async(db, req.openid, req.amount,
                                  "recharge", req.description)


@router.get("/usage", response_model=UsageResponse)
async def get_usage(openid: str = Query(""), db=Depends(get_db)):
    """用户用量统计"""
    stats = await get_usage_stats_async(db, openid)
    return UsageResponse(**stats)


# ── 管理后台 ──

@router.get("/admin/users")
async def admin_users(db=Depends(get_db)):
    """管理后台：所有用户资源概览"""
    return {"ok": True, "users": await admin_all_users_resources(db)}
