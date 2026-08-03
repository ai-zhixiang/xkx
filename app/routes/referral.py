"""推广返佣 — API 路由
推广码生成、关系绑定、佣金记录与查询
推送通知：新用户绑定/开通会员时通知推荐人 Bot
"""
import json
import os
import random
import string
from datetime import datetime, date
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import asyncpg
import httpx

DATABASE_URL = "postgresql://lucky:lucky_pass@localhost:5432/weclawd"

router = APIRouter(prefix="/api/referral", tags=["推广返佣"])

# ── 返佣比例（百分比） ──
LEVEL1_RATE = 15  # 一级 15%
LEVEL2_RATE = 5   # 二级 5%
POINTS_PER_YUAN = 100  # 1元 = 100虾点

# ── Keepalive 推送地址 ──
_KEEPALIVE_URL = "http://127.0.0.1:9100/api/send"


async def _push_to_referrer(referrer_openid: str, message: str):
    """查询 referrer 的 Bot 会话并推送通知"""
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            cb = await conn.fetchrow(
                "SELECT channel_user_id FROM channel_bindings "
                "WHERE openid = $1 AND channel_type = 'ilink' "
                "AND channel_user_id IS NOT NULL LIMIT 1",
                referrer_openid
            )
            if not cb:
                return
            uid = cb["channel_user_id"]

            ba = await conn.fetchrow(
                "SELECT bot_id FROM bot_accounts "
                "WHERE (user_id = $1 OR user_id LIKE $2) "
                "AND is_active = true LIMIT 1",
                uid, uid.split("@")[0] + "%"
            )
            if not ba:
                return

            async with httpx.AsyncClient(timeout=5) as _hc:
                await _hc.post(_KEEPALIVE_URL, json={
                    "bot_id": ba["bot_id"],
                    "to_user": uid,
                    "text": message
                })
        finally:
            await conn.close()
    except Exception:
        pass  # 推送失败不影响主流程


class CodeRequest(BaseModel):
    openid: str
    nickname: Optional[str] = None


class BindRequest(BaseModel):
    referee_openid: str
    referral_code: str
    referee_nickname: str = ""  # 推荐人微信昵称（前端传入）


def _generate_code(length=8) -> str:
    """生成唯一推广码"""
    chars = string.ascii_uppercase + string.digits
    return 'XK' + ''.join(random.choices(chars, k=length))


# ── 1. 获取/生成推广码 ──

@router.post("/code")
async def get_or_create_code(data: CodeRequest):
    """获取用户的推广码，没有则生成"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # 查询用户 subscriber
        sub = await conn.fetchrow(
            "SELECT id, nickname FROM subscribers WHERE openid = $1", data.openid
        )
        if not sub:
            raise HTTPException(404, "用户不存在，请先开通享客虾会员")

        subscriber_id = sub["id"]

        # 查已有推广码
        code_row = await conn.fetchrow(
            "SELECT code FROM referral_codes WHERE subscriber_id = $1", subscriber_id
        )
        if code_row:
            code = code_row["code"]
        else:
            # 生成唯一码
            for _ in range(10):
                code = _generate_code()
                exists = await conn.fetchrow(
                    "SELECT id FROM referral_codes WHERE code = $1", code
                )
                if not exists:
                    break
            await conn.execute(
                "INSERT INTO referral_codes (subscriber_id, code) VALUES ($1, $2)",
                subscriber_id, code
            )

        # 统计数据
        stats = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE level = 1) AS level1_count,
                COUNT(*) FILTER (WHERE level = 2) AS level2_count
            FROM referral_relations
            WHERE referrer_subscriber_id = $1
        """, subscriber_id)

        # 佣金统计
        comm = await conn.fetchrow("""
            SELECT
                COALESCE(SUM(points_awarded) FILTER (WHERE status = 'paid'), 0) AS total_earned,
                COALESCE(SUM(points_awarded) FILTER (WHERE status = 'pending'), 0) AS pending_earned,
                COUNT(*) FILTER (WHERE status = 'paid') AS paid_count,
                COUNT(*) FILTER (WHERE status = 'pending') AS pending_count
            FROM referral_commissions
            WHERE referrer_subscriber_id = $1
        """, subscriber_id)

        return {
            "code": code,
            "link": f"https://ai.pangoozn.com/xiakexia?ref={code}",
            "stats": {
                "一级推广": stats["level1_count"],
                "二级推广": stats["level2_count"]
            },
            "commissions": {
                "已到账虾点": comm["total_earned"],
                "待结算虾点": comm["pending_earned"],
                "已结算笔数": comm["paid_count"],
                "待结算笔数": comm["pending_count"]
            }
        }
    finally:
        await conn.close()


# ── 2. 绑定推广关系（新用户打开推广链接时调用） ──

@router.post("/bind")
async def bind_referral(data: BindRequest):
    """新用户首次访问时绑定推广关系"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # 查找推广码对应的用户
        ref = await conn.fetchrow("""
            SELECT rc.subscriber_id, rc.code AS referrer_code, s.openid AS referrer_openid, s.nickname
            FROM referral_codes rc
            JOIN subscribers s ON s.id = rc.subscriber_id
            WHERE rc.code = $1
        """, data.referral_code)
        if not ref:
            raise HTTPException(404, "推广码无效")

        referrer_id = ref["subscriber_id"]
        referrer_openid = ref["referrer_openid"]
        referrer_nick = ref["nickname"] or "虾友"
        referrer_code = ref["referrer_code"] or data.referral_code

        # 不能自己推广自己
        if data.referee_openid == referrer_openid:
            raise HTTPException(400, "不能推广自己")

        # 检查是否已被其他推广人绑定
        existing = await conn.fetchrow(
            "SELECT id FROM referral_relations WHERE referee_openid = $1",
            data.referee_openid
        )
        if existing:
            return {"status": "ok", "message": "已被推广绑定", "bound": True}

        # 查 referee 是否已有 subscriber
        ref_sub = await conn.fetchrow(
            "SELECT id, nickname FROM subscribers WHERE openid = $1", data.referee_openid
        )
        referee_sub_id = ref_sub["id"] if ref_sub else None
        # 昵称优先级：前端传入 > DB > 默认
        referee_nick = data.referee_nickname or (ref_sub["nickname"] if ref_sub else "虾友")

        # 绑定一级关系
        await conn.execute("""
            INSERT INTO referral_relations
                (referee_openid, referrer_subscriber_id, referrer_openid, level, referee_subscriber_id)
            VALUES ($1, $2, $3, 1, $4)
        """, data.referee_openid, referrer_id, referrer_openid, referee_sub_id)

        # 查找二级关系：referrer 的上线
        parent = await conn.fetchrow(
            "SELECT referrer_subscriber_id, referrer_openid FROM referral_relations "
            "WHERE referee_openid = $1 AND level = 1",
            referrer_openid
        )
        if parent:
            await conn.execute("""
                INSERT INTO referral_relations
                    (referee_openid, referrer_subscriber_id, referrer_openid, level, referee_subscriber_id)
                VALUES ($1, $2, $3, 2, $4)
            """, data.referee_openid, parent["referrer_subscriber_id"],
                parent["referrer_openid"], referee_sub_id)

        # 如果 referee 已有 subscriber，立即尝试结算新人奖励
        rewards = 50  # 新人奖励 50 虾点
        if referee_sub_id:
            sub = await conn.fetchrow(
                "SELECT xiake_points FROM subscribers WHERE id = $1", referee_sub_id
            )
            if sub is not None:
                new_points = sub["xiake_points"] + rewards
                await conn.execute(
                    "UPDATE subscribers SET xiake_points = $1 WHERE id = $2",
                    new_points, referee_sub_id
                )

        # ── 推送通知给推荐人 ──
        _push_msg = (
            f"🎉 你的推广带来新用户！\n"
            f"「{referee_nick}」通过你的链接添加了享客虾 Bot\n\n"
            f"继续分享 → https://ai.pangoozn.com/promo/referral?code={referrer_code}"
        )
        await _push_to_referrer(referrer_openid, _push_msg)

        return {
            "status": "ok",
            "message": f"推广关系绑定成功（一级）",
            "referrer": referrer_openid,
            "reward": rewards if referee_sub_id else 0,
            "二级上线": bool(parent)
        }
    finally:
        await conn.close()


# ── 3. 查询推广信息 ──

@router.get("/info")
async def referral_info(openid: str = Query(..., description="用户 openid")):
    """查询用户的推广信息（自动生成推广码）"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        sub = await conn.fetchrow(
            "SELECT id, nickname, avatar_url FROM subscribers WHERE openid = $1", openid
        )
        if not sub:
            raise HTTPException(404, "用户不存在")

        sub_id = sub["id"]

        # 推广码 — 没有则自动生成
        code_row = await conn.fetchrow(
            "SELECT code FROM referral_codes WHERE subscriber_id = $1", sub_id
        )
        if code_row:
            code = code_row["code"]
        else:
            import string as _rs, random as _rr
            _chars = _rs.ascii_uppercase + _rs.digits
            for _ in range(10):
                _new_code = "XK" + "".join(_rr.choices(_chars, k=8))
                _exists = await conn.fetchrow("SELECT id FROM referral_codes WHERE code = $1", _new_code)
                if not _exists:
                    break
            await conn.execute(
                "INSERT INTO referral_codes (subscriber_id, code) VALUES ($1, $2)",
                sub_id, _new_code
            )
            code = _new_code

        # 一级人数
        level1 = await conn.fetchval(
            "SELECT COUNT(*) FROM referral_relations "
            "WHERE referrer_subscriber_id = $1 AND level = 1", sub_id
        )
        level2 = await conn.fetchval(
            "SELECT COUNT(*) FROM referral_relations "
            "WHERE referrer_subscriber_id = $1 AND level = 2", sub_id
        )

        # 佣金
        comm = await conn.fetchrow("""
            SELECT
                COALESCE(SUM(points_awarded) FILTER (WHERE status = 'paid'), 0) AS paid_pts,
                COALESCE(SUM(points_awarded) FILTER (WHERE status = 'pending'), 0) AS pending_pts,
                COUNT(*) FILTER (WHERE status = 'paid') AS paid_cnt,
                COUNT(*) FILTER (WHERE status = 'pending') AS pending_cnt
            FROM referral_commissions
            WHERE referrer_subscriber_id = $1
        """, sub_id)

        return {
            "code": code,
            "referral_code": code,
            "nickname": sub.get("nickname") or "",
            "avatar_url": sub.get("avatar_url") or "",
            "link": f"https://ai.pangoozn.com/xiakexia?ref={code}" if code else None,
            "referral_count": level1,
            "total_commission": comm["paid_pts"] + comm["pending_pts"],
            "level2_count": level2,
            "推广人数": {"一级": level1, "二级": level2},
            "佣金": {
                "已到账虾点": comm["paid_pts"],
                "待结算虾点": comm["pending_pts"],
                "已结算笔数": comm["paid_cnt"],
                "待结算笔数": comm["pending_cnt"]
            }
        }
    finally:
        await conn.close()


# ── 4. 佣金记录明细 ──

@router.get("/commissions")
async def commission_list(
    openid: str = Query(..., description="用户 openid"),
    status: str = Query("all", description="过滤: all/paid/pending")
):
    """查询佣金明细"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        sub = await conn.fetchrow(
            "SELECT id, nickname, avatar_url FROM subscribers WHERE openid = $1", openid
        )
        if not sub:
            raise HTTPException(404, "用户不存在")

        status_filter = ""
        if status in ("paid", "pending"):
            status_filter = f"AND rc.status = '{status}'"

        rows = await conn.fetch(f"""
            SELECT
                rc.id, rc.level, rc.order_amount, rc.commission_rate,
                rc.points_awarded, rc.status, rc.created_at, rc.settled_at,
                COALESCE(s_referee.nickname, '未知用户') AS referee_nickname
            FROM referral_commissions rc
            LEFT JOIN subscribers s_referee ON s_referee.id = rc.referee_subscriber_id
            WHERE rc.referrer_subscriber_id = $1 {status_filter}
            ORDER BY rc.created_at DESC
            LIMIT 50
        """, sub["id"])

        return {
            "total": len(rows),
            "items": [
                {
                    "id": r["id"],
                    "level": f"{'一级' if r['level'] == 1 else '二级'}",
                    "order_amount": r["order_amount"],
                    "rate": f"{r['commission_rate']}%",
                    "points": r["points_awarded"],
                    "status": "已到账" if r["status"] == "paid" else "待结算",
                    "from": r["referee_nickname"],
                    "created_at": str(r["created_at"]) if r["created_at"] else None,
                    "settled_at": str(r["settled_at"]) if r["settled_at"] else None
                }
                for r in rows
            ]
        }
    finally:
        await conn.close()


# ── 5. 订单结算佣金（支付回调调用） ──

@router.post("/settle")
async def settle_commission(order_id: int = Query(...)):
    """订单支付成功后结算推广佣金"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # 查询订单
        order = await conn.fetchrow(
            "SELECT id, subscriber_id, amount, status FROM sub_orders WHERE id = $1",
            order_id
        )
        if not order or order["status"] != "PAID":
            raise HTTPException(400, "订单不存在或未支付")

        subscriber_id = order["subscriber_id"]

        # 查 referee 的 openid
        sub = await conn.fetchrow(
            "SELECT openid, nickname FROM subscribers WHERE id = $1", subscriber_id
        )
        if not sub:
            raise HTTPException(404, "用户不存在")

        openid = sub["openid"]
        referee_nick = sub["nickname"] or "虾友"

        # 查一级推广关系
        ref1 = await conn.fetchrow(
            "SELECT referrer_subscriber_id, referrer_openid, level "
            "FROM referral_relations WHERE referee_openid = $1 AND level = 1",
            openid
        )

        results = []
        order_amount = order["amount"]

        if ref1:
            # 一级佣金：15%
            rate = LEVEL1_RATE
            points = int(order_amount * rate * POINTS_PER_YUAN / 10000)

            if points > 0 and ref1["referrer_subscriber_id"]:
                existing = await conn.fetchrow(
                    "SELECT id FROM referral_commissions "
                    "WHERE referrer_subscriber_id = $1 AND order_id = $2 AND level = 1",
                    ref1["referrer_subscriber_id"], order_id
                )
                if not existing:
                    await conn.execute("""
                        INSERT INTO referral_commissions
                            (referrer_subscriber_id, referee_subscriber_id, order_id,
                             level, order_amount, commission_rate, points_awarded, status)
                        VALUES ($1, $2, $3, 1, $4, $5, $6, 'paid')
                    """, ref1["referrer_subscriber_id"], subscriber_id, order_id,
                        order_amount, rate, points)

                    await conn.execute("""
                        UPDATE subscribers SET xiake_points = xiake_points + $1
                        WHERE id = $2
                    """, points, ref1["referrer_subscriber_id"])

                    results.append({
                        "level": 1, "rate": f"{rate}%",
                        "points": points, "to_subscriber": ref1["referrer_subscriber_id"]
                    })

            # 查二级关系
            ref2 = await conn.fetchrow(
                "SELECT referrer_subscriber_id, referrer_openid, level "
                "FROM referral_relations WHERE referee_openid = $1 AND level = 1",
                ref1["referrer_openid"]
            )
            if ref2:
                rate2 = LEVEL2_RATE
                points2 = int(order_amount * rate2 * POINTS_PER_YUAN / 10000)
                if points2 > 0 and ref2["referrer_subscriber_id"]:
                    existing2 = await conn.fetchrow(
                        "SELECT id FROM referral_commissions "
                        "WHERE referrer_subscriber_id = $1 AND order_id = $2 AND level = 2",
                        ref2["referrer_subscriber_id"], order_id
                    )
                    if not existing2:
                        await conn.execute("""
                            INSERT INTO referral_commissions
                                (referrer_subscriber_id, referee_subscriber_id, order_id,
                                 level, order_amount, commission_rate, points_awarded, status)
                            VALUES ($1, $2, $3, 2, $4, $5, $6, 'paid')
                        """, ref2["referrer_subscriber_id"], subscriber_id, order_id,
                            order_amount, rate2, points2)

                        await conn.execute("""
                            UPDATE subscribers SET xiake_points = xiake_points + $1
                            WHERE id = $2
                        """, points2, ref2["referrer_subscriber_id"])

                        results.append({
                            "level": 2, "rate": f"{rate2}%",
                            "points": points2, "to_subscriber": ref2["referrer_subscriber_id"]
                        })

                    # ── 推送战绩通知给一级推荐人 ──
                    _total_pts = sum(r["points"] for r in results if r["level"] == 1)
                    if _total_pts > 0:
                        # 查一级推荐人的推广码
                        _ref1_code = await conn.fetchval(
                            "SELECT code FROM referral_codes WHERE subscriber_id = $1",
                            ref1["referrer_subscriber_id"]
                        )
                        _ref1_code = _ref1_code or ""
                # 查一级推荐人当前总战绩
                _l1 = await conn.fetchval(
                    "SELECT COUNT(*) FROM referral_relations "
                    "WHERE referrer_subscriber_id = $1 AND level = 1",
                    ref1["referrer_subscriber_id"]
                )
                _pts = await conn.fetchval(
                    "SELECT COALESCE(SUM(points_awarded), 0) FROM referral_commissions "
                    "WHERE referrer_subscriber_id = $1 AND status = 'paid'",
                    ref1["referrer_subscriber_id"]
                )
                _push_msg = (
                    f"🎉 推广战绩更新！\n"
                    f"「{referee_nick}」开通了享客虾会员\n"
                    f"你获得 {_total_pts} 虾点佣金 💰\n\n"
                    f"📊 累计战绩\n"
                    f"邀请人数：{_l1} 人\n"
                    f"总佣金：{_pts} 虾点\n\n"
                    f"继续推广 → https://ai.pangoozn.com/promo/referral?code={_ref1_code}"
                )
                await _push_to_referrer(ref1["referrer_openid"], _push_msg)

        return {
            "status": "ok",
            "order_id": order_id,
            "amount": order_amount,
            "settled": results,
            "total_points_awarded": sum(r["points"] for r in results)
        }
    finally:
        await conn.close()


# ── 6. 验证推广码 ──

@router.get("/promo-code")
async def promo_code(openid: str = Query(..., description="用户 openid（取自 OAuth）")):
    """播放器页专用：创建/查询订阅者 + 生成推广码，返回推广信息"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        sub = await conn.fetchrow("SELECT id, nickname FROM subscribers WHERE openid = $1", openid)
        if not sub:
            # 新建订阅者：优先用已有真实昵称（auth_center users 表），避免占位符
            _nick = f"虾友{openid[-6:]}"
            _av = None
            try:
                _ac = await asyncpg.connect("postgresql://lucky:lucky_pass@localhost:5432/auth_center")
                try:
                    _real = await _ac.fetchrow(
                        "SELECT u.nickname, u.avatar_url FROM users u JOIN openid_mappings om ON om.phone = u.phone WHERE om.openid = $1 LIMIT 1",
                        openid)
                finally:
                    await _ac.close()
            except Exception:
                _real = None
            if _real and _real["nickname"] and _real["nickname"] != "微信用户":
                _nick = _real["nickname"]
                _av = _real["avatar_url"]
            await conn.execute(
                "INSERT INTO subscribers (openid, nickname, avatar_url, daily_push, plan_id, status, started_at, expires_at, created_at, updated_at) "
                "VALUES ($1, $2, $3, false, 8, 'VISITOR', CURRENT_DATE, CURRENT_DATE + 365, NOW(), NOW())",
                openid, _nick, _av
            )
            sub = await conn.fetchrow("SELECT id, nickname FROM subscribers WHERE openid = $1", openid)

        sub_id = sub["id"]

        # 推广码
        code_row = await conn.fetchrow(
            "SELECT code FROM referral_codes WHERE subscriber_id = $1", sub_id
        )
        if code_row:
            code = code_row["code"]
        else:
            _chars = string.ascii_uppercase + string.digits
            for _ in range(10):
                _new_code = "XK" + "".join(random.choices(_chars, k=8))
                _exists = await conn.fetchrow("SELECT id FROM referral_codes WHERE code = $1", _new_code)
                if not _exists:
                    break
            await conn.execute(
                "INSERT INTO referral_codes (subscriber_id, code) VALUES ($1, $2)",
                sub_id, _new_code
            )
            code = _new_code

        # 统计
        level1 = await conn.fetchval(
            "SELECT COUNT(*) FROM referral_relations WHERE referrer_subscriber_id = $1 AND level = 1", sub_id
        )

        # 判断是否有 Bot
        has_bot = False
        try:
            cb = await conn.fetchrow(
                "SELECT channel_user_id FROM channel_bindings WHERE openid = $1 AND is_active = true LIMIT 1",
                openid
            )
            if cb:
                bot = await conn.fetchrow(
                    "SELECT id FROM bot_accounts WHERE user_id = $1 AND is_active = true LIMIT 1",
                    cb["channel_user_id"]
                )
                if bot:
                    has_bot = True
        except:
            pass

        return {
            "openid": openid,
            "nickname": sub["nickname"] or f"虾友{openid[-4:]}",
            "code": code,
            "referral_link": f"https://ai.pangoozn.com/player/promo-haica?ref={code}",
            "bind_link": f"https://ai.pangoozn.com/xkx/bind?ref={code}",
            "referral_count": level1,
            "has_bot": has_bot
        }
    finally:
        await conn.close()


@router.get("/verify-code")
async def verify_code(code: str = Query(..., description="推广码")):
    """验证推广码是否有效"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow("""
            SELECT rc.code, s.nickname, s.openid
            FROM referral_codes rc
            JOIN subscribers s ON s.id = rc.subscriber_id
            WHERE rc.code = $1
        """, code)
        if not row:
            return {"valid": False}
        return {
            "valid": True,
            "nickname": row["nickname"],
            "openid": row["openid"]
        }
    finally:
        await conn.close()
