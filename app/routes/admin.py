"""享客虾 Bot 管理后台 API"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import asyncpg, httpx, os
from datetime import datetime, date

DATABASE_URL = "postgresql://lucky:lucky_pass@localhost:5432/weclawd"
router = APIRouter()

class LoginReq(BaseModel):
    password: str

class PushReq(BaseModel):
    openid: str
    text: str = "🦞 这是来自管理后台的测试消息"

@router.post("/login")
async def admin_login(req: LoginReq):
    pwd = os.environ.get("ADMIN_PASSWORD", "")
    if not pwd:
        return JSONResponse({"ok": False, "error": "未设置管理密码"}, status_code=500)
    if req.password == pwd:
        return {"ok": True, "token": "admin_verified"}
    return JSONResponse({"ok": False, "error": "密码错误"}, status_code=403)

@router.get("/bots")
async def admin_bots():
    """Bot 列表：基础信息 + 会员状态"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("""
            SELECT b.bot_id, b.nickname, b.user_id, b.is_active, b.platform, b.created_at AS bot_created,
                   s.plan_id, s.status AS member_status, s.expires_at,
                   s.xiake_points, s.total_points_consumed, s.total_points_recharged,
                   cb.nickname AS wx_nickname, cb.cb_openid
            FROM bot_accounts b
            LEFT JOIN LATERAL (
                SELECT cb.nickname, cb.openid AS cb_openid
                FROM channel_bindings cb
                WHERE (cb.channel_user_id = b.user_id OR cb.channel_user_id || '@im.wechat' = b.user_id)
                  AND cb.is_active = true
                ORDER BY (cb.openid LIKE 'oHTeBx%') DESC, cb.bound_at DESC
                LIMIT 1
            ) cb ON true
            LEFT JOIN subscribers s ON s.openid = cb.cb_openid
            WHERE b.is_active = true
            ORDER BY b.created_at DESC
        """)
        today = date.today()
        return {"bots": [{
            "bot_id": r["bot_id"],
            "nickname": r["wx_nickname"] or r["nickname"] or "未知",
            "user_id": r["user_id"],
            "openid": r["cb_openid"] or "",
            "created_at": str(r["bot_created"]) if r["bot_created"] else None,
            "is_active": r["is_active"],
            "platform": r["platform"],
            "member": bool(r["plan_id"] and r["plan_id"] > 0 and r["member_status"] == "ACTIVE" and r["expires_at"] and r["expires_at"] >= today),
            "member_status": r["member_status"],
            "expires_at": str(r["expires_at"]) if r["expires_at"] else None,
            "total_points": r["xiake_points"] or 0,
            "consumed_points": r["total_points_consumed"] or 0,
            "total_recharged": r["total_points_recharged"] or 0
        } for r in rows]}
    finally:
        await conn.close()

@router.get("/subscribers")
async def admin_subscribers(q: str = ""):
    """用户列表，支持按昵称/openid搜索"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        if q:
            rows = await conn.fetch("""
                SELECT s.*, r.code AS referral_code,
                       EXISTS (SELECT 1 FROM channel_bindings cb WHERE cb.openid = s.openid) AS has_bot
                FROM subscribers s
                LEFT JOIN referral_codes r ON r.subscriber_id = s.id
                WHERE s.nickname ILIKE $1 OR s.openid ILIKE $1
                ORDER BY s.id DESC LIMIT 50
            """, f"%{q}%")
        else:
            rows = await conn.fetch("""
                SELECT s.*, r.code AS referral_code,
                       EXISTS (SELECT 1 FROM channel_bindings cb WHERE cb.openid = s.openid) AS has_bot
                FROM subscribers s
                LEFT JOIN referral_codes r ON r.subscriber_id = s.id
                ORDER BY s.id DESC LIMIT 50
            """)
        return {"subscribers": [{
            "openid": r["openid"],
            "nickname": r["nickname"] or "虾友",
            "avatar_url": r["avatar_url"] or "",
            "phone": r["phone"] or "",
            "plan_id": r["plan_id"] or 0,
            "total_points_consumed": r["total_points_consumed"] or 0,
            "status": r["status"],
            "daily_push": r.get("daily_push", False),
            "referral_code": r["referral_code"] or "",
            "has_bot": r["has_bot"],
            "origin": r.get("origin", "") or "",
            "created_at": str(r["created_at"]) if r["created_at"] else str(r["updated_at"]) if r["updated_at"] else "",
            "updated_at": str(r["updated_at"]) if r["updated_at"] else ""
        } for r in rows]}
    finally:
        await conn.close()

@router.get("/user/{openid}")
async def admin_user(openid: str):
    """查用户全链路：subscribers → channel_bindings → bot_accounts"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        sub = await conn.fetchrow("SELECT * FROM subscribers WHERE openid=$1", openid)
        cb = await conn.fetch("SELECT * FROM channel_bindings WHERE openid=$1", openid)
        bots = []
        for c in cb:
            cuid = c["channel_user_id"]
            b = await conn.fetchrow(
                "SELECT * FROM bot_accounts WHERE user_id=$1 OR user_id LIKE $2",
                f"{cuid}@im.wechat", f"{cuid}@%"
            )
            if b:
                bots.append(dict(b))
        rr = await conn.fetch("""
            SELECT rr.*, s.nickname AS referrer_nick
            FROM referral_relations rr
            LEFT JOIN subscribers s ON s.openid = rr.referrer_openid
            WHERE rr.referee_openid = $1
        """, openid)
        ref_me = await conn.fetch("""
            SELECT s.openid, s.nickname FROM referral_relations rr
            JOIN subscribers s ON s.openid = rr.referee_openid
            WHERE rr.referrer_openid = (SELECT openid FROM subscribers WHERE openid=$1)
            LIMIT 20
        """, openid)
        return {
            "subscriber": dict(sub) if sub else None,
            "channel_bindings": [dict(c) for c in cb],
            "bots": bots,
            "referral_from": [dict(r) for r in rr],
            "referral_to": [dict(r) for r in ref_me]
        }
    finally:
        await conn.close()

@router.post("/push")
async def admin_push(req: PushReq):
    """向用户推送测试消息"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        cb = await conn.fetchrow(
            "SELECT channel_user_id FROM channel_bindings WHERE openid=$1 AND is_active=true LIMIT 1",
            req.openid
        )
        if not cb:
            return JSONResponse({"ok": False, "error": "未找到绑定记录"}, status_code=404)
        cuid = cb["channel_user_id"]
        br = await conn.fetchrow(
            "SELECT bot_id FROM bot_accounts WHERE (user_id=$1 OR user_id LIKE $2) AND is_active=true LIMIT 1",
            cuid, f"{cuid}@%"
        )
        if not br:
            return JSONResponse({"ok": False, "error": "未找到 Bot"}, status_code=404)
        to_user = f"{cuid}@im.wechat" if "@" not in cuid else cuid
        async with httpx.AsyncClient(timeout=5) as hc:
            kr = await hc.post("http://127.0.0.1:9100/api/send", json={
                "bot_id": br["bot_id"],
                "to_user": to_user,
                "text": req.text
            })
            return {"ok": kr.status_code == 200, "status": kr.status_code}
    finally:
        await conn.close()

@router.get("/stats")
async def admin_stats():
    """概览统计：访客/虾友/会员/今日新增"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # OAuth总数 = 所有授权过的用户
        oauth_count = await conn.fetchval("SELECT COUNT(*) FROM subscribers")
        # Bot总数 = 加了Bot的用户
        bot_user_count = await conn.fetchval("""
            SELECT COUNT(*) FROM subscribers s
            WHERE EXISTS (SELECT 1 FROM channel_bindings cb WHERE cb.openid = s.openid)
        """)
        # 互动数 = 消耗过虾点的用户
        interactive_count = await conn.fetchval("""
            SELECT COUNT(*) FROM subscribers WHERE total_points_consumed > 0
        """)
        member_subs = await conn.fetchval(
            "SELECT COUNT(*) FROM subscribers WHERE plan_id > 0 AND status='ACTIVE'"
        )
        today_new_subs = await conn.fetchval(
            "SELECT COUNT(*) FROM subscribers WHERE created_at::date = CURRENT_DATE"
        )
        # 来源统计
        origin_rows = await conn.fetch("""
            SELECT origin, COUNT(*) AS cnt FROM subscribers
            WHERE origin IS NOT NULL AND origin != ''
            GROUP BY origin ORDER BY cnt DESC LIMIT 10
        """)
        # 今日来源
        today_origin = await conn.fetch("""
            SELECT origin, COUNT(*) AS cnt FROM subscribers
            WHERE origin IS NOT NULL AND origin != ''
              AND created_at::date = CURRENT_DATE
            GROUP BY origin ORDER BY cnt DESC LIMIT 5
        """)
        # 转化率
        bot_rate = round(bot_user_count / oauth_count * 100, 1) if oauth_count > 0 else 0
        interactive_rate = round(interactive_count / bot_user_count * 100, 1) if bot_user_count > 0 else 0
        member_rate = round(member_subs / interactive_count * 100, 1) if interactive_count > 0 else 0
        return {
            "oauth_count": oauth_count,
            "bot_user_count": bot_user_count,
            "interactive_count": interactive_count,
            "member_subscribers": member_subs,
            "today_new_subs": today_new_subs,
            "bot_rate": bot_rate,
            "interactive_rate": interactive_rate,
            "member_rate": member_rate,
            "origin_stats": [dict(r) for r in origin_rows],
            "today_origin": [dict(r) for r in today_origin]
        }
    finally:
        await conn.close()