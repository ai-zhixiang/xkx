"""
享客虾 · 资源管理核心模块
虾点扣费、磁盘限额、用量统计

双接口：sync（asyncpg，给 keepalive 用）+ async（SQLAlchemy，给 FastAPI 用）
"""

import logging
from datetime import date, datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)

# ── 定价表（内存缓存，首次加载后每 5 分钟刷新） ──
_PRICING = {}
_LAST_PRICING_LOAD = datetime.min

POINTS_PRICING = {
    "chat": 1,
    "make_card": 2,
    "ai_song": 50,
    "rvc": 30,
    "vision": 2,
}

# ============================================================
#  Sync 接口（给 keepalive_service.py 用，asyncpg）
# ============================================================

async def get_member_points(conn, openid: str) -> dict:
    """查会员虾点信息。返回 {xiake_points, points_expires_at, is_member}"""
    row = await conn.fetchrow(
        "SELECT s.xiake_points, s.points_expires_at, s.status, s.expires_at "
        "FROM subscribers s WHERE s.openid = $1 AND s.status IN ('ACTIVE','TRIAL') "
        "ORDER BY s.id DESC LIMIT 1",
        openid
    )
    if not row:
        return {"xiake_points": 0, "points_expires_at": None, "is_member": False}
    status = str(row["status"]).upper()
    exp = row["expires_at"]
    is_member = status in ("ACTIVE", "TRIAL") and exp and exp >= date.today()
    return {
        "xiake_points": row["xiake_points"] or 0,
        "points_expires_at": row["points_expires_at"],
        "is_member": is_member,
        "expires_at": exp,
    }


async def check_and_deduct_points(conn, openid: str, action: str = "chat",
                                   points: Optional[int] = None) -> dict:
    """
    检查并扣除虾点。
    返回 {"ok": bool, "remaining": int, "need": int, "message": str}
    """
    need = points or POINTS_PRICING.get(action, 1)
    info = await get_member_points(conn, openid)
    if not info["is_member"]:
        return {"ok": False, "remaining": 0, "need": need,
                "message": "非会员用户请使用免费额度"}
    current = info["xiake_points"]
    if current < need:
        return {"ok": False, "remaining": current, "need": need,
                "message": f"🦞 虾点不足（剩余 {current}，需要 {need}）"}

    # 扣除
    new_balance = current - need
    await conn.execute(
        "UPDATE subscribers SET xiake_points = $1, total_points_consumed = total_points_consumed + $2 "
        "WHERE openid = $3 AND xiake_points = $4",
        new_balance, need, openid, current
    )
    # 写流水
    sub_id = await conn.fetchval(
        "SELECT id FROM subscribers WHERE openid = $1", openid
    )
    if sub_id:
        await conn.execute(
            "INSERT INTO points_transactions (subscriber_id, tx_type, amount, balance_after, description) "
            "VALUES ($1, 'consume', $2, $3, $4)",
            sub_id, -need, new_balance, action
        )
    return {"ok": True, "remaining": new_balance, "need": need, "message": ""}


async def add_points_sync(conn, openid: str, amount: int,
                          tx_type: str = "recharge", description: str = "") -> dict:
    """加虾点（充值/奖励）。返回 {ok, balance_after}"""
    row = await conn.fetchrow(
        "UPDATE subscribers SET xiake_points = xiake_points + $1, "
        "total_points_recharged = total_points_recharged + $2 "
        "WHERE openid = $3 RETURNING xiake_points",
        amount, amount, openid
    )
    if not row:
        return {"ok": False, "balance_after": 0}
    new_balance = row["xiake_points"]
    sub_id = await conn.fetchval(
        "SELECT id FROM subscribers WHERE openid = $1", openid
    )
    if sub_id:
        await conn.execute(
            "INSERT INTO points_transactions (subscriber_id, tx_type, amount, balance_after, description) "
            "VALUES ($1, $2, $3, $4, $5)",
            sub_id, tx_type, amount, new_balance, description
        )
    return {"ok": True, "balance_after": new_balance}


async def get_disk_usage_sync(conn, openid: str) -> dict:
    """查磁盘使用情况。返回 {used_bytes, quota_bytes, available_bytes, used_pct}"""
    row = await conn.fetchrow(
        "SELECT disk_used_bytes, disk_quota_bytes FROM subscribers WHERE openid = $1",
        openid
    )
    if not row:
        return {"used_bytes": 0, "quota_bytes": 2147483648,
                "available_bytes": 2147483648, "used_pct": 0}
    used = row["disk_used_bytes"] or 0
    quota = row["disk_quota_bytes"] or 2147483648
    return {
        "used_bytes": used,
        "quota_bytes": quota,
        "available_bytes": max(0, quota - used),
        "used_pct": round(used / quota * 100, 1) if quota > 0 else 0,
    }


async def check_disk_quota_sync(conn, openid: str, needed_bytes: int) -> dict:
    """检查磁盘配额是否足够"""
    info = await get_disk_usage_sync(conn, openid)
    if info["available_bytes"] < needed_bytes:
        return {"ok": False, "message": f"磁盘空间不足（剩余 {info['available_bytes']//1048576}MB）",
                **info}
    return {"ok": True, "message": "", **info}


async def get_usage_stats_sync(conn, openid: str) -> dict:
    """用量统计"""
    sub_id = await conn.fetchval(
        "SELECT id FROM subscribers WHERE openid = $1", openid
    )
    result = {
        "points_balance": 0, "points_consumed_today": 0,
        "points_consumed_month": 0, "recharged_total": 0,
        "disk_used_mb": 0, "disk_quota_mb": 2048,
        "chat_count_today": 0, "is_member": False,
    }
    if not sub_id:
        return result

    # 基本信息
    row = await conn.fetchrow(
        "SELECT xiake_points, total_points_consumed, total_points_recharged, "
        "disk_used_bytes, disk_quota_bytes, status, expires_at "
        "FROM subscribers WHERE id = $1", sub_id
    )
    if row:
        result["points_balance"] = row["xiake_points"] or 0
        result["recharged_total"] = row["total_points_recharged"] or 0
        result["disk_used_mb"] = round((row["disk_used_bytes"] or 0) / 1048576, 1)
        result["disk_quota_mb"] = round((row["disk_quota_bytes"] or 2147483648) / 1048576, 1)
        st = str(row["status"]).upper()
        exp = row["expires_at"]
        result["is_member"] = st in ("ACTIVE", "TRIAL") and exp and exp >= date.today()

    # 今日消耗
    td = date.today()
    today_row = await conn.fetchrow(
        "SELECT COALESCE(SUM(ABS(amount)), 0) FROM points_transactions "
        "WHERE subscriber_id = $1 AND tx_type = 'consume' AND created_at::date = $2",
        sub_id, td
    )
    result["points_consumed_today"] = today_row[0] if today_row else 0

    # 本月消耗
    first_of_month = td.replace(day=1)
    month_row = await conn.fetchrow(
        "SELECT COALESCE(SUM(ABS(amount)), 0) FROM points_transactions "
        "WHERE subscriber_id = $1 AND tx_type = 'consume' AND created_at::date >= $2",
        sub_id, first_of_month
    )
    result["points_consumed_month"] = month_row[0] if month_row else 0

    # 今日对话次数
    import asyncpg
    chat_row = await conn.fetchrow(
        "SELECT used FROM daily_quota WHERE user_id = $1 AND quota_date = $2",
        openid, td
    )
    result["chat_count_today"] = chat_row["used"] if chat_row else 0

    return result


# ============================================================
#  Async 接口（给 FastAPI 用，SQLAlchemy）
# ============================================================

async def get_member_points_async(db, openid: str) -> dict:
    """查会员虾点信息（SQLAlchemy 版）"""
    from sqlalchemy import text as sa_text
    from datetime import date
    row = await db.execute(
        sa_text("SELECT s.xiake_points, s.points_expires_at, s.status, s.expires_at, s.id "
                "FROM subscribers s WHERE s.openid LIKE :oid AND s.status IN ('ACTIVE','TRIAL') "
                "ORDER BY s.id DESC LIMIT 1"),
        {"oid": openid}
    )
    r = row.fetchone()
    if not r:
        return {"xiake_points": 0, "points_expires_at": None, "is_member": False, "subscriber_id": None}
    status = str(r[2]).upper()
    exp = r[3]
    is_member = status in ("ACTIVE", "TRIAL") and exp and exp >= date.today()
    return {
        "xiake_points": r[0] or 0,
        "points_expires_at": str(r[1]) if r[1] else "",
        "is_member": is_member,
        "subscriber_id": r[4],
    }


async def check_and_deduct_async(db, openid: str, action: str = "chat",
                                  points: Optional[int] = None) -> dict:
    """异步版扣点（给 API 调用）"""
    from sqlalchemy import text as sa_text
    need = points or POINTS_PRICING.get(action, 1)
    info = await get_member_points_async(db, openid)
    if not info["is_member"]:
        return {"ok": False, "remaining": 0, "need": need, "message": "非会员"}
    if info["xiake_points"] < need:
        return {"ok": False, "remaining": info["xiake_points"], "need": need,
                "message": f"🦞 虾点不足（剩余 {info['xiake_points']}，需要 {need}）"}

    new_balance = info["xiake_points"] - need
    sub_id = info["subscriber_id"]

    await db.execute(
        sa_text("UPDATE subscribers SET xiake_points = :bal, "
                "total_points_consumed = total_points_consumed + :need "
                "WHERE id = :sid AND xiake_points = :old"),
        {"bal": new_balance, "need": need, "sid": sub_id,
         "old": info["xiake_points"]}
    )
    await db.execute(
        sa_text("INSERT INTO points_transactions (subscriber_id, tx_type, amount, balance_after, description) "
                "VALUES (:sid, 'consume', :amt, :bal, :desc)"),
        {"sid": sub_id, "amt": -need, "bal": new_balance, "desc": action}
    )
    return {"ok": True, "remaining": new_balance, "need": need, "message": ""}


async def add_points_async(db, openid: str, amount: int,
                           tx_type: str = "recharge", description: str = "") -> dict:
    """异步版加虾点"""
    from sqlalchemy import text as sa_text
    row = await db.execute(
        sa_text("UPDATE subscribers SET xiake_points = xiake_points + :amt, "
                "total_points_recharged = total_points_recharged + :amt "
                "WHERE openid LIKE :oid RETURNING xiake_points, id"),
        {"amt": amount, "oid": openid + "%"}
    )
    r = row.fetchone()
    if not r:
        return {"ok": False, "balance_after": 0}
    await db.execute(
        sa_text("INSERT INTO points_transactions (subscriber_id, tx_type, amount, balance_after, description) "
                "VALUES (:sid, :tx, :amt, :bal, :desc)"),
        {"sid": r[1], "tx": tx_type, "amt": amount, "bal": r[0], "desc": description}
    )
    return {"ok": True, "balance_after": r[0]}


async def get_usage_stats_async(db, openid: str) -> dict:
    """异步版用量统计"""
    from sqlalchemy import text as sa_text
    from datetime import date, datetime

    result = {
        "points_balance": 0, "points_consumed_today": 0,
        "points_consumed_month": 0, "recharged_total": 0,
        "points_expires_at": "",
        "disk_used_mb": 0, "disk_quota_mb": 2048,
        "chat_count_today": 0, "is_member": False,
        "expires_at": "",
    }

    # 查会员信息
    row = await db.execute(
        sa_text("SELECT id, xiake_points, total_points_consumed, total_points_recharged, "
                "points_expires_at, disk_used_bytes, disk_quota_bytes, "
                "status, expires_at "
                "FROM subscribers WHERE openid LIKE :oid ORDER BY id DESC LIMIT 1"),
        {"oid": openid + "%"}
    )
    r = row.fetchone()
    if not r:
        return result

    sid, points, consumed, recharged, p_exp, d_used, d_quota, status, exp = r
    result["points_balance"] = points or 0
    result["recharged_total"] = recharged or 0
    result["disk_used_mb"] = round((d_used or 0) / 1048576, 1)
    result["disk_quota_mb"] = round((d_quota or 2147483648) / 1048576, 1)
    st = str(status).upper()
    result["expires_at"] = str(exp) if exp else None
    result["points_expires_at"] = str(p_exp) if p_exp else None
    result["is_member"] = st in ("ACTIVE", "TRIAL") and exp and exp >= date.today()

    if not sid:
        return result

    td = date.today()
    # 今日消耗
    today_r = await db.execute(
        sa_text("SELECT COALESCE(SUM(ABS(amount)), 0) FROM points_transactions "
                "WHERE subscriber_id = :sid AND tx_type = 'consume' AND created_at::date = :td"),
        {"sid": sid, "td": td}
    )
    result["points_consumed_today"] = today_r.scalar() or 0

    # 本月消耗
    month_r = await db.execute(
        sa_text("SELECT COALESCE(SUM(ABS(amount)), 0) FROM points_transactions "
                "WHERE subscriber_id = :sid AND tx_type = 'consume' AND created_at::date >= :fm"),
        {"sid": sid, "fm": td.replace(day=1)}
    )
    result["points_consumed_month"] = month_r.scalar() or 0

    # 今日对话
    chat_r = await db.execute(
        sa_text("SELECT used FROM daily_quota WHERE user_id LIKE :uid AND quota_date = :td"),
        {"uid": openid + "%", "td": td}
    )
    cr = chat_r.fetchone()
    result["chat_count_today"] = cr[0] if cr else 0

    return result


async def admin_all_users_resources(db) -> list:
    """管理后台：所有用户资源概览"""
    from sqlalchemy import text as sa_text
    from datetime import date
    rows = await db.execute(
        sa_text("""
            SELECT s.openid, s.nickname, s.xiake_points, s.total_points_consumed,
                   s.total_points_recharged, s.disk_used_bytes, s.disk_quota_bytes,
                   s.status, s.expires_at, s.points_expires_at,
                   COALESCE(cb.channel_user_id, '') as bot_channel
            FROM subscribers s
            LEFT JOIN LATERAL (
                SELECT channel_user_id FROM channel_bindings
                WHERE channel_user_id LIKE s.openid || '%' LIMIT 1
            ) cb ON true
            ORDER BY s.updated_at DESC NULLS LAST
            LIMIT 100
        """)
    )
    results = []
    for r in rows.fetchall():
        results.append({
            "openid": r[0],
            "nickname": r[1] or r[0][:8],
            "xiake_points": r[2] or 0,
            "total_consumed": r[3] or 0,
            "total_recharged": r[4] or 0,
            "disk_used_mb": round((r[5] or 0) / 1048576, 1),
            "disk_quota_mb": round((r[6] or 2147483648) / 1048576, 1),
            "status": r[7],
            "expires_at": str(r[8]) if r[8] else "",
            "points_expires_at": str(r[9]) if r[9] else "",
            "bot_bound": bool(r[10]),
        })
    return results
