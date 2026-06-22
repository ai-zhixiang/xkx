"""
享客虾额度管理 — 每日消息配额 + 付费引导

并发安全设计：
- PostgreSQL 原子操作保障计数器准确（避免内存竞态）
- 只读查用户信息走 10 秒本地缓存
- 配额写入直接走 DB UPSERT，无锁

免费用户：50 条/天
会员用户：按套餐每月配额 / 30 ≈ 每日配额
"""

import time
import logging
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

FREE_DAILY_LIMIT = 50

# 本地缓存: {user_id: {"limit": 50, "is_member": false, "cached_at": 12345}}
_subscriber_cache = {}
CACHE_TTL = 10  # 缓存 10 秒


def _today() -> str:
    return date.today().isoformat()


def _pg() -> "Engine":
    """获取同步 PG 连接（避免 asyncio 冲突）"""
    from sqlalchemy import create_engine
    return create_engine("postgresql://lucky:lucky_pass@localhost:5432/weclawd", pool_pre_ping=True)


def _get_or_create_subscriber_sync(user_id: str) -> dict:
    """
    同步查/创建订阅记录。
    返回 {daily_limit: int, is_member: bool}
    """
    engine = _pg()
    try:
        with engine.begin() as conn:
            # 查已有记录
            row = conn.execute(
                __import__('sqlalchemy').text(
                    "SELECT id, plan_id, messages_limit, status, expires_at FROM subscribers WHERE openid = :uid"
                ),
                {"uid": user_id},
            ).fetchone()

            if row:
                _id, plan_id, msg_limit, status, expires_at = row
                is_member = (
                    str(status).upper() in ("ACTIVE", "TRIAL")
                    and expires_at
                    and expires_at >= date.today()
                )
                limit = msg_limit or FREE_DAILY_LIMIT
            else:
                # 新用户自动创建
                now = datetime.now()
                conn.execute(
                    __import__('sqlalchemy').text(
                        """INSERT INTO subscribers 
                        (openid, nickname, status, started_at, expires_at, messages_limit, messages_used, created_at)
                        VALUES (:uid, :nick, 'ACTIVE', :now, :exp, :limit, 0, :now)"""
                    ),
                    {"uid": user_id, "nick": f"Bot_{user_id[:8]}", "now": now,
                     "exp": now.replace(year=now.year + 10), "limit": FREE_DAILY_LIMIT},
                )
                limit = FREE_DAILY_LIMIT
                is_member = False

            if is_member and plan_id and (not msg_limit or msg_limit <= 0):
                # 不限量套餐
                daily_limit = 999999
            elif is_member and msg_limit and msg_limit > 0:
                daily_limit = max(1, msg_limit // 30)
            else:
                daily_limit = FREE_DAILY_LIMIT

            return {"daily_limit": daily_limit, "is_member": is_member}
    except Exception as e:
        logger.error(f"[Quota] DB 查询失败: {e}")
        # 降级：按免费用户处理
        return {"daily_limit": FREE_DAILY_LIMIT, "is_member": False}
    finally:
        engine.dispose()


def _cached_or_fetch(user_id: str) -> dict:
    """带缓存的用户信息查询（10 秒 TTL）"""
    cached = _subscriber_cache.get(user_id)
    now = time.time()
    if cached and (now - cached["cached_at"]) < CACHE_TTL:
        return cached

    info = _get_or_create_subscriber_sync(user_id)
    info["cached_at"] = time.time()
    _subscriber_cache[user_id] = info
    return info


def _consume_quota_sync(user_id: str) -> dict:
    """
    原子消耗一条额度。
    返回: {"ok": true, "remaining": N}  或 {"ok": false}
    """
    engine = _pg()
    try:
        with engine.begin() as conn:
            today = _today()
            # 用行锁原子操作：INSERT ... ON CONFLICT DO UPDATE
            conn.execute(
                __import__('sqlalchemy').text(
                    """INSERT INTO daily_quota (user_id, quota_date, used)
                       VALUES (:uid, :dt, 1)
                       ON CONFLICT (user_id, quota_date)
                       DO UPDATE SET used = daily_quota.used + 1"""
                ),
                {"uid": user_id, "dt": today},
            )

            # 读当前用量
            row = conn.execute(
                __import__('sqlalchemy').text(
                    "SELECT used FROM daily_quota WHERE user_id = :uid AND quota_date = :dt FOR UPDATE"
                ),
                {"uid": user_id, "dt": today},
            ).fetchone()

            used = row[0] if row else 0

            # 查用户等级确定限额
            sub = conn.execute(
                __import__('sqlalchemy').text(
                    "SELECT plan_id, messages_limit, status, expires_at FROM subscribers WHERE openid = :uid"
                ),
                {"uid": user_id},
            ).fetchone()

            if sub:
                is_member = (str(sub[2]).upper() in ("ACTIVE", "TRIAL")
                             and sub[3] and sub[3] >= date.today())
                limit = sub[1] or FREE_DAILY_LIMIT if is_member else FREE_DAILY_LIMIT
                daily_limit = 999999 if (is_member and (not limit or limit <= 0)) else max(1, limit // 30 if is_member and limit > 0 else FREE_DAILY_LIMIT)
            else:
                daily_limit = FREE_DAILY_LIMIT

            remaining = max(0, daily_limit - used)
            return {"ok": remaining > 0, "remaining": remaining, "used": used, "limit": daily_limit}
    except Exception as e:
        logger.error(f"[Quota] 消耗配额失败: {e}")
        return {"ok": True, "remaining": 1, "used": 0, "limit": FREE_DAILY_LIMIT}
    finally:
        engine.dispose()


def check_and_consume(user_id: str) -> dict:
    """检查并消耗一条额度（并发安全，DB 原子操作）"""
    result = _consume_quota_sync(user_id)

    if not result["ok"]:
        logger.info(f"[Quota] 用户 {user_id[:20]} 额度已用完 (used={result['used']}/{result['limit']})")
        return {
            "ok": False,
            "quota": {"limit": result["limit"], "used": result["used"], "remaining": 0, "is_member": False},
            "message": get_upgrade_message(),
        }

    # 判断是否首次使用
    is_first = result["used"] == 1 and result.get("remaining", 0) > 0

    # 首次使用添加欢迎语
    welcome = None
    if is_first:
        welcome = (
            "🦞 欢迎来到享客虾！🎉

"
            "我是你的 AI 创作伙伴，先绑定手机号激活完整体验：
"
            "https://hai.pangoozn.com/static/xiake_landing.html

"
            "绑定后你可以：
"
            "🎵 AI 写歌 · 说出你的故事，AI 为你谱曲
"
            "💝 AI 嗨卡 · 照片+诗+歌，传心意
"
            "🤖 智能聊天 · 随叫随到"
        )

    quota = {
        "limit": result["limit"],
        "used": result["used"],
        "remaining": result["remaining"],
        "is_member": False,  # simplified
    }

    resp = {"ok": True, "quota": quota}

    if is_first:
        resp["welcome"] = get_welcome_message(quota["remaining"])

    return resp


def get_upgrade_message() -> str:
    return (
        "🦞 **享客虾 · 免费额度已用完**\n\n"
        "你今天免费 50 条对话已用尽，明天重置。\n\n"
        "👉 开通会员继续畅聊：\n"
        "   • 基础版 ¥9.9/月 — 500条/月\n"
        "   • 标准版 ¥19.9/月 — 2000条/月\n\n"
        "开通方式：打开 https://hai.pangoozn.com/xkx/ 选择套餐\n\n"
        "感谢体验！🙏"
    )


def get_welcome_message(remaining: int) -> str:
    return (
        "🦞 **欢迎来到享客虾！**\n\n"
        "我是你的微信私人AI秘书，可以：\n"
        "• 💬 自由聊天、提问、咨询\n"
        "• 🔍 搜索信息、查资料\n"
        "• 📄 生成文档、报告\n"
        "• 🎨 创意写作、策划\n\n"
        f"📊 今日免费剩余：**{remaining} 条**\n"
        "   · 免费用户 50 条/天\n"
        "   · 会员不限量\n\n"
        "开通会员 → https://hai.pangoozn.com/xkx/"
    )
