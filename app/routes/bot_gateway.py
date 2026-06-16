#!/usr/bin/env python3
"""
bot_gateway.py — 微信 Bot 网关
接收 unified_connector 的 webhook，转发到 Hermes Bridge (:8642)
"""
import asyncio
import json
import logging
import os
import sys
import time
import httpx
from datetime import datetime
from pathlib import Path
from typing import Optional

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("bot-gateway")

app = FastAPI(title="Bot Gateway")

# ── 配置 ──
HERMES_API_URL = "http://127.0.0.1:8642/v1/chat/completions"
HERMES_API_KEY = "sk-c86b55fe6d46473fa3deec2f75d0448b"
DB_DSN = "postgresql://lucky:lucky_pass@localhost:5432/xiaolongxia"
MODEL = "hermes-shenzhen"

CONVERSATION_TABLE = "conversation_messages"
HISTORY_LIMIT = 30

# ── 四级调度配置 ──
# L0: 暗号匹配（零成本）
KEYWORD_L0 = {
    "天王盖地虎": "OpenClaw 是SB！",
    "宝塔镇河妖": "微侠真牛逼！",
    "微侠真牛逼": "天王盖地虎！同志！",
    "openclaw是sb": "宝塔镇河妖！收到！",
    "openclaw is sx": "宝塔镇河妖！收到！",
    "openclaw是sx": "宝塔镇河妖！收到！",
}

# L1: 轻量关键词 → 直调 DeepSeek，无历史，max_tokens=128
KEYWORD_L1 = {
    "hi", "hello", "在吗", "在不在", "ping", "test",
    "你好", "嗨", "hey",
    "查余额", "余额",
}

# L3: 深度任务关键词 → 全量上下文，max_tokens=2048
KEYWORD_L3 = {
    "写", "生成", "分析", "总结", "翻译", "解释",
    "创建", "编写", "输出", "报告", "制作",
    "pdf", "文档",
}

def _strip_punct(text: str) -> str:
    import re
    return re.sub(r'[，。！？、；：""''\s]', '', text).lower()

def _classify_intent(text: str):
    """返回 (level: int, immediate_reply: str|None)"""
    stripped = _strip_punct(text)
    # L0: 暗号
    for key, reply in KEYWORD_L0.items():
        if _strip_punct(key) == stripped or key.lower() in stripped:
            return (0, reply)
    # L1: 轻量
    if stripped in {_strip_punct(w) for w in KEYWORD_L1}:
        return (1, None)
    # L3: 深度
    for kw in KEYWORD_L3:
        if kw in text:
            return (3, None)
    # 默认 L2
    return (2, None)


# ── Bot 账号查询 ──
async def get_bot_config(bot_id: str) -> Optional[dict]:
    """从 DB 查 Bot 配置（backend 字段决定路由到 Hermes 还是 DeepSeek）"""
    try:
        conn = await asyncpg.connect(DB_DSN)
        try:
            row = await conn.fetchrow(
                "SELECT bot_id, backend, bot_token FROM bot_accounts WHERE bot_id = $1 AND is_active = true",
                bot_id
            )
            if row:
                return dict(row)
            return None
        finally:
            await conn.close()
    except Exception as e:
        logger.error(f"get_bot_config({bot_id[:20]}): {e}")
        return None


# ── 对话历史 ──
async def load_history(user_id: str, bot_id: str = "", limit: int = HISTORY_LIMIT) -> list:
    """加载最近对话历史"""
    try:
        conn = await asyncpg.connect(DB_DSN)
        try:
            # 带 bot_id 隔离查询
            if bot_id:
                rows = await conn.fetch(
                    f"SELECT role, content FROM {CONVERSATION_TABLE} "
                    "WHERE user_id = $1 AND bot_id = $2 ORDER BY id DESC LIMIT $3",
                    user_id, bot_id, limit
                )
            else:
                rows = await conn.fetch(
                    f"SELECT role, content FROM {CONVERSATION_TABLE} "
                    "WHERE user_id = $1 ORDER BY id DESC LIMIT $2",
                    user_id, limit
                )
            return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
        finally:
            await conn.close()
    except Exception as e:
        logger.debug(f"load_history({user_id[:20]}): {e}")
        return []


async def save_message(user_id: str, role: str, content: str, bot_id: str = ""):
    """保存一条对话记录"""
    try:
        conn = await asyncpg.connect(DB_DSN)
        try:
            if bot_id:
                await conn.execute(
                    f"INSERT INTO {CONVERSATION_TABLE} (user_id, role, content, bot_id, created_at) VALUES ($1, $2, $3, $4, NOW())",
                    user_id, role, content, bot_id
                )
            else:
                await conn.execute(
                    f"INSERT INTO {CONVERSATION_TABLE} (user_id, role, content, created_at) VALUES ($1, $2, $3, NOW())",
                    user_id, role, content
                )
        finally:
            await conn.close()
    except Exception as e:
        logger.debug(f"save_message({user_id[:20]}): {e}")


# ── Hermes 调用 ──
async def _call_hermes(
    prompt: str,
    user_id: str,
    user_nickname: str = "",
    openid: str = "",
    user_account_id: int = None,
    media_path: str = "",
    bot_id: str = "",
    level: int = 2,
) -> str:
    """调用 Hermes Bridge (:8642) — 带重试 + 超时保护"""
    media_hint = ""
    if media_path:
        media_hint = f"\n用户发来媒体文件: {media_path}。你可以读取并处理它。"

    system_prompt = (
        f"当前用户: {user_nickname or '铭道'} | OpenID: {(openid or '')[:16]}...{media_hint}\n"
        f"注意：你的 persona 和记忆已加载。用 MEDIA:/path/to/file 格式回复可将文件推送到微信对话框。\n"
        f"交互原则：用户说什么就做什么，不要反问确认。例如用户说「输出 PDF」就直接生成 PDF 输出，不要问「要包含什么内容」。"
    )

    # 用 bot_id:openid 作为 session_id，隔离不同 Bot 的上下文
    session_id = f"{bot_id}:{openid or user_id}"

    # 按级别调整 max_tokens
    max_tokens = 2048 if level == 3 else 512

    try:
        # 重试 2 次：首次 + 1 次退避重试
        MAX_ATTEMPTS = 2
        last_error = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                async with httpx.AsyncClient(timeout=300) as client:
                    r = await client.post(
                        HERMES_API_URL,
                        headers={
                            "Authorization": f"Bearer {HERMES_API_KEY}",
                            "Content-Type": "application/json",
                            "x-session-id": session_id,
                        },
                        json={
                            "model": MODEL,
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": prompt},
                            ],
                            "max_tokens": max_tokens,
                        },
                    )
                    if r.status_code == 200:
                        data = r.json()
                        reply = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                        if reply:
                            return reply
                        return "抱歉，暂时无法回复。"
                    # 服务端错误可重试
                    if r.status_code >= 500 and attempt + 1 < MAX_ATTEMPTS:
                        logger.warning(f"Hermes {r.status_code} (attempt {attempt+1}), 重试...")
                        await asyncio.sleep(2 ** attempt)
                        continue
                    logger.error(f"Hermes API error {r.status_code}: {r.text[:200]}")
                    return f"抱歉，处理出错（{r.status_code}），请稍后再试。"
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = e
                if attempt + 1 < MAX_ATTEMPTS:
                    logger.warning(f"Hermes 请求失败 (attempt {attempt+1}): {e}, 重试...")
                    await asyncio.sleep(2 ** attempt)
                    continue
        # 所有重试都失败
        logger.warning(f"Hermes 全部重试失败 session_id={session_id[:20]}: {last_error}")
        return "处理超时，请稍后重试。"

    except Exception as e:
        logger.error(f"_call_hermes: {e}")
        return f"处理出错，请稍后再试。"


# ── 路由分发 ──
async def _route_to_ai(
    bot_id: str,
    user_id: str,
    content: str,
    user_nickname: str = "",
    openid: str = "",
    user_account_id: int = None,
    media_path: str = "",
) -> str:
    """按 backend 字段路由：hermes / deepseek，叠加四级调度"""
    # 先分类级别
    level, immediate_reply = _classify_intent(content)
    logger.info(f"📊 调度级别 L{level}: \"{content[:30]}\"")
    
    # L0: 暗号 → 直接回复，零成本
    if level == 0 and immediate_reply:
        return immediate_reply

    bot_cfg = await get_bot_config(bot_id)
    backend = (bot_cfg or {}).get("backend", "hermes")

    # L1 + DeepSeek backend: 直调 DeepSeek 无历史
    if backend == "deepseek" or level == 1:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {HERMES_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "deepseek-chat",
                        "messages": [{"role": "user", "content": content}],
                        "max_tokens": 128 if level == 1 else 512,
                    },
                )
                if r.status_code == 200:
                    return r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                logger.warning(f"DeepSeek 直调 {r.status_code}: {r.text[:100]}")
        except Exception as e:
            logger.error(f"直调失败: {e}")
            if level == 1:
                return "在的 👋"
            return "暂时不可用。"

    # L2/L3 走 Hermes（带级别的上下文量）
    return await _call_hermes(content, user_id, user_nickname, openid, user_account_id, media_path, bot_id, level)


# ── Webhook ──
@app.post("/api/bot/webhook")
async def bot_webhook(request: Request):
    """接收 unified_connector 转发的消息"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid JSON"}, status_code=400)

    bot_id = data.get("bot_id", "")
    user_id = data.get("user_id", "")
    content = data.get("content", "")
    media_path = data.get("media_path", "")
    nickname = data.get("nickname", data.get("user_nickname", ""))
    openid = data.get("openid", user_id)  # fallback: 用 user_id 当 openid
    user_account_id = data.get("user_account_id")

    if not bot_id or not user_id:
        return JSONResponse({"success": False, "error": "Missing bot_id or user_id"}, status_code=400)

    if not content and not media_path:
        return JSONResponse({"success": False, "error": "Empty content"}, status_code=400)

    logger.info(f"📩 {bot_id[:16]} <- {user_id[:20]}: {content[:40]}")

    try:
        response = await _route_to_ai(
            bot_id, user_id, content, nickname, openid, user_account_id, media_path
        )
        logger.info(f"📤 -> {response[:60]}")
        return JSONResponse({"success": True, "response": response})

    except Exception as e:
        logger.error(f"webhook 处理异常: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "bot-gateway"}


# ── 启动 ──
if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
    logger.info(f"🚀 Bot Gateway 启动 :{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


@router.post("/sync-subscriber")
async def sync_subscriber(data: dict):
    """主站支付成功后调用，同步会员状态到 weclawd DB"""
    openid = data.get("openid", "")
    nickname = data.get("nickname", "")
    phone = data.get("phone", "")
    expires_at_str = data.get("expires_at", "")
    
    if not openid:
        return {"success": False, "error": "缺少 openid"}
    
    try:
        from app.models import AsyncSessionLocal as _asf
        from sqlalchemy import text as _t
        from datetime import datetime, date
        
        expires_date = datetime.fromisoformat(expires_at_str).date() if expires_at_str else date.today()
        
        async with _asf() as _s:
            # 先查是否存在
            row = await _s.execute(
                _t("SELECT id, expires_at FROM subscribers WHERE openid = :oid"),
                {"oid": openid},
            )
            existing = row.fetchone()
            
            if existing:
                # 叠加过期时间
                new_expires = max(existing[1], expires_date) if existing[1] else expires_date
                await _s.execute(
                    _t("UPDATE subscribers SET expires_at = :ea, status = 'active', nickname = :nick, updated_at = NOW() WHERE id = :id"),
                    {"ea": new_expires, "nick": nickname, "id": existing[0]},
                )
            else:
                # 新建
                await _s.execute(
                    _t("""INSERT INTO subscribers (openid, nickname, phone, plan_id, status, started_at, expires_at, created_at, updated_at)
                        VALUES (:oid, :nick, :phone, 1, 'active', CURRENT_DATE, :ea, NOW(), NOW())"""),
                    {"oid": openid, "nick": nickname, "phone": phone, "ea": expires_date},
                )
            await _s.commit()
            logger.info(f"[Sync] 会员同步成功: openid={openid[:12]}, expires={expires_date}")
            return {"success": True, "message": f"会员已同步至 {expires_date}"}
    except Exception as e:
        logger.error(f"[Sync] 会员同步失败: {e}")
        return {"success": False, "error": str(e)}
